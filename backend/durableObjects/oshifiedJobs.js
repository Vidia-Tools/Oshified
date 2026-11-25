const JOB_LEASE = 30 * 60 * 1000;
const DAILY_LIMIT = 5;
const ARTIFACTS = { portrait: 'png', still: 'jpg', clip: 'mp4' };
const isTerminal = (status) => status === 'complete' || status === 'failed';
export const canRetryClip = (job, now = Date.now()) =>
	job.status === 'failed' && !job.timedOut && Boolean(job.artifacts.still && job.portraitKey) &&
	!job.retryUsed && now < job.createdAt + JOB_LEASE * 2;


export class OshifiedJobs {
	constructor(state) {
		this.storage = state.storage;
	}

	async fetch(request) {
		const url = new URL(request.url);
		if (url.pathname === '/job' && request.method === 'GET') {
			const job = await this.storage.get('job:' + url.searchParams.get('id'));
			if (!job) return new Response(null, { status: 404 });
			if (!isTerminal(job.status) && job.expiresAt <= Date.now()) {
				return Response.json({ ...job, status: 'failed', timedOut: true, error: 'Generation timed out' });
			}
			return Response.json(job);
		}

		if (url.pathname === '/reserve' && request.method === 'POST') {
			const record = await request.json();
			return this.storage.transaction(async (txn) => {
				const now = Date.now();
				const activeKey = 'active:' + record.owner;
				const active = await txn.get(activeKey);
				if (active?.expiresAt > now) return new Response(null, { status: 409 });
				const day = new Date(now).toISOString().slice(0, 10);
				const quotaKey = 'quota:' + day + ':' + record.owner;
				const count = (await txn.get(quotaKey)) || 0;
				if (count >= DAILY_LIMIT) return new Response(null, { status: 429 });
				const expiresAt = now + JOB_LEASE;
				const job = { ...record, attempt: 1, status: 'queued', createdAt: now, expiresAt, artifacts: {}, progress: { phase: 'starting' } };
				await txn.put({
					[activeKey]: { id: record.id, expiresAt },
					[quotaKey]: count + 1,
					['job:' + record.id]: job,
				});
				return Response.json({ id: record.id });
			});
		}

		if (url.pathname === '/retry' && request.method === 'POST') {
			const { id, access } = await request.json();
			return this.storage.transaction(async (txn) => {
				const key = 'job:' + id;
				const job = await txn.get(key);
				if (!job || !access || job.access !== access) return new Response(null, { status: 404 });
				const now = Date.now();
				const activeKey = 'active:' + job.owner;
				const active = await txn.get(activeKey);
				if (!canRetryClip(job, now) || active?.expiresAt > now) return new Response(null, { status: 409 });
				job.retryUsed = true;
				job.attempt += 1;
				job.status = 'queued';
				job.error = null;
				job.progress = { phase: 'starting', stageName: 'clip' };
				job.expiresAt = now + JOB_LEASE;
				await txn.put({ [key]: job, [activeKey]: { id, expiresAt: job.expiresAt } });
				return Response.json(job);
			});
		}

		if (url.pathname === '/share' && request.method === 'POST') {
			const { id, access } = await request.json();
			return this.storage.transaction(async (txn) => {
				const key = 'job:' + id;
				const job = await txn.get(key);
				if (!job || !access || job.access !== access || !job.artifacts.clip) {
					return new Response(null, { status: 404 });
				}
				if (!job.shareAccess) {
					job.shareAccess = crypto.randomUUID();
					await txn.put(key, job);
				}
				return Response.json({ access: job.shareAccess });
			});
		}
		if (url.pathname === '/progress' && request.method === 'POST') {
			const { generation_id: id, attempt, eventType, progressData } = await request.json();
			return this.storage.transaction(async (txn) => {
				const key = 'job:' + id;
				const job = await txn.get(key);
				if (!job) return new Response(null, { status: 404 });
				if (attempt !== job.attempt) return new Response('Stale attempt');
				if (isTerminal(job.status)) return new Response('Already finished');
				if (job.expiresAt <= Date.now()) return new Response(null, { status: 410 });

				if (eventType === 'oshified_artifact') {
					const { artifact, key: objectKey } = progressData || {};
					if (!Object.hasOwn(ARTIFACTS, artifact) ||
						objectKey !== `oshified/${id}/${artifact}.${ARTIFACTS[artifact]}`) {
						return new Response('Invalid artifact', { status: 400 });
					}
					if (artifact === 'portrait') job.portraitKey = objectKey;
					else job.artifacts[artifact] = objectKey;
					job.status = job.artifacts.still && job.artifacts.clip ? 'complete' : 'running';
				} else if (eventType === 'error') {
					job.status = 'failed';
					job.error = String(progressData?.error || 'Generation failed').slice(0, 200);
				} else if (eventType === 'model_preparation') {
					job.status = 'running';
					job.progress = {
						phase: progressData?.phase === 'downloading' ? 'downloading' : 'loading',
						bytesReceived: progressData?.bytesReceived,
						totalBytes: progressData?.totalBytes,
						stageName: progressData?.stageName,
					};
				} else if (eventType === 'stage_start' || eventType === 'progress') {
					job.status = 'running';
					job.progress = { phase: 'generating', stageName: progressData?.stageName };
				} else {
					return new Response('Ignored');
				}
				await txn.put(key, job);
				if (isTerminal(job.status)) {
					const activeKey = 'active:' + job.owner;
					if ((await txn.get(activeKey))?.id === id) await txn.delete(activeKey);
				}
				return new Response('Recorded');
			});
		}
		return new Response(null, { status: 404 });
	}
}
