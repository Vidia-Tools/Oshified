import { expect, test, vi } from 'vitest';
import { OshifiedJobs } from '../../durableObjects/oshifiedJobs.js';

function durableStorage() {
	const entries = new Map();
	const storage = {
		get: async (key) => structuredClone(entries.get(key)),
		put: async (key, value) => {
			for (const [name, data] of typeof key === 'string' ? [[key, value]] : Object.entries(key)) {
				entries.set(name, structuredClone(data));
			}
		},
		delete: async (key) => entries.delete(key),
		transaction: async (callback) => callback(storage),
	};
	return storage;
}

const post = (jobs, path, data) => jobs.fetch(new Request('https://fixture.test' + path, {
	method: 'POST', body: JSON.stringify(data),
}));
const read = async (jobs, id) => (await jobs.fetch(new Request('https://fixture.test/job?id=' + id))).json();
const extensions = { portrait: "png", still: "jpg", clip: "mp4" };
const artifact = (jobs, id, kind, attempt = 1) => post(jobs, '/progress', {
	generation_id: id, attempt, eventType: 'oshified_artifact', progressData: {
		artifact: kind, key: `oshified/${id}/attempt-${attempt}/${kind}.${extensions[kind]}`,
	},
});

test('persists a partial result and completes only when both artifacts arrive', async () => {
	const storage = durableStorage();
	const id = 'job';
	const owner = 'requester';
	const job = { id, owner, access: 'private', inputAccess: 'worker' };
	let jobs = new OshifiedJobs({ storage });
	expect((await post(jobs, '/reserve', job)).status).toBe(200);
	expect((await post(jobs, '/reserve', { ...job, id: 'second' })).status).toBe(409);

	await post(jobs, '/progress', {
		generation_id: id, attempt: 1, eventType: 'model_preparation',
		progressData: { phase: 'downloading', bytesReceived: 5, totalBytes: 10 },
	});
	jobs = new OshifiedJobs({ storage });
	expect((await read(jobs, id)).progress.bytesReceived).toBe(5);
	expect((await artifact(jobs, id, 'still')).status).toBe(200);
	expect((await read(jobs, id)).status).toBe('running');
	expect((await post(jobs, '/share', { id, access: 'private' })).status).toBe(404);
	expect((await artifact(jobs, id, 'clip')).status).toBe(200);
	expect((await artifact(jobs, id, 'clip')).status).toBe(200);
	expect((await read(jobs, id)).status).toBe('complete');
	expect((await post(jobs, '/reserve', { ...job, id: 'second' })).status).toBe(200);

	expect((await post(jobs, '/share', { id, access: 'wrong' })).status).toBe(404);
	const response = await post(jobs, '/share', { id, access: 'private' });
	expect((await response.json()).access).not.toBe('private');
});


test('keeps a saved portrait through one clip-only retry and ignores old callbacks', async () => {
	const jobs = new OshifiedJobs({ storage: durableStorage() });
	const id = 'retry-job';
	const access = 'owner-token';
	await post(jobs, '/reserve', { id, owner: 'guest', access, inputAccess: 'worker' });
	await artifact(jobs, id, 'portrait');
	await artifact(jobs, id, 'still');
	await post(jobs, '/progress', {
		generation_id: id, attempt: 1, eventType: 'error', progressData: { error: 'Clip failed' },
	});
	expect((await read(jobs, id)).status).toBe('failed');
	expect((await post(jobs, '/retry', { id, access: 'wrong' })).status).toBe(404);
	const retry = await post(jobs, '/retry', { id, access });
	const queued = await retry.json();
	expect(queued.attempt).toBe(2);
	expect(queued.portraitKey).toBe(`oshified/${id}/attempt-1/portrait.png`);
	expect(await (await artifact(jobs, id, 'clip', 1)).text()).toBe('Stale attempt');
	const wrongKey = await post(jobs, '/progress', {
		generation_id: id, attempt: 2, eventType: 'oshified_artifact',
		progressData: { artifact: 'clip', key: `oshified/${id}/attempt-1/clip.mp4` },
	});
	expect(wrongKey.status).toBe(400);
	expect((await read(jobs, id)).status).toBe('queued');
	expect((await post(jobs, '/retry', { id, access })).status).toBe(409);
	expect((await artifact(jobs, id, 'clip', 2)).status).toBe(200);
	expect((await read(jobs, id)).artifacts).toEqual({
		still: `oshified/${id}/attempt-1/still.jpg`, clip: `oshified/${id}/attempt-2/clip.mp4`,
	});
});

test('keeps a cold job active past 30 minutes and uses its saved lease for retry', async () => {
	const minute = 60 * 1000;
	const start = Date.UTC(2026, 10, 25);
	const clock = vi.spyOn(Date, 'now').mockReturnValue(start);
	try {
		const storage = durableStorage();
		const id = 'cold-job';
		const access = 'owner-token';
		const job = { id, owner: 'requester', access, inputAccess: 'worker' };
		const jobs = new OshifiedJobs({ storage }, { OSHIFIED_JOB_LEASE_MINUTES: '50' });
		expect((await post(jobs, '/reserve', job)).status).toBe(200);
		expect((await read(jobs, id)).expiresAt).toBe(start + 50 * minute);
		clock.mockReturnValue(start + 34 * minute);
		expect((await post(jobs, '/reserve', { ...job, id: 'second' })).status).toBe(409);
		expect((await artifact(jobs, id, 'portrait')).status).toBe(200);
		expect((await artifact(jobs, id, 'still')).status).toBe(200);
		await post(jobs, '/progress', { generation_id: id, attempt: 1, eventType: 'error' });

		const restarted = new OshifiedJobs({ storage });
		const retry = await post(restarted, '/retry', { id, access });
		expect(retry.status).toBe(200);
		expect((await retry.json()).expiresAt).toBe(start + 84 * minute);

		clock.mockReturnValue(start + 84 * minute);
		expect((await read(restarted, id)).timedOut).toBe(true);
		expect((await artifact(restarted, id, 'clip', 2)).status).toBe(410);
		expect((await post(restarted, '/reserve', { ...job, id: 'second' })).status).toBe(200);
		expect((await read(restarted, 'second')).expiresAt).toBe(start + 129 * minute);
	} finally {
		clock.mockRestore();
	}
});
