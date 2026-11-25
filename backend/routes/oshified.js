import { jsonResponse } from '../utils/response.js';
import { runEndpointAsync } from '../services/runpod.js';
import { canRetryClip } from '../durableObjects/oshifiedJobs.js';

const IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);
const AUDIO_TYPES = new Set(['audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/mp4', 'audio/ogg', 'audio/webm']);
const MAX_IMAGE = 10 * 1024 * 1024;
const MAX_AUDIO = 15 * 1024 * 1024;
const MAX_REQUEST = MAX_IMAGE + MAX_AUDIO + 16 * 1024;
const MODES = new Set(['vtuber-to-real', 'real-to-vtuber']);
const ID_PATTERN = /^oshified_[a-f0-9]{32}$/;

export const oshifiedJobs = (env) => env.OSHIFIED_JOBS.get(env.OSHIFIED_JOBS.idFromName('jobs'));

function validAsset(value) {
	try {
		const url = new URL(value);
		return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
	} catch {
		return null;
	}
}

function isYoutubeLink(value) {
	const url = validAsset(value);
	if (!url) return false;
	const parsed = new URL(url);
	if (parsed.hostname === 'youtu.be') return /^\/[\w-]{11}\/?$/.test(parsed.pathname);
	if (!['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(parsed.hostname)) return false;
	return (parsed.pathname === '/watch' && /^[\w-]{11}$/.test(parsed.searchParams.get('v') || '')) ||
		/^\/shorts\/[\w-]{11}\/?$/.test(parsed.pathname);
}

function reportError(message, status) {
	return jsonResponse({ success: false, message }, status);
}

async function checkTurnstile(token, ip, secret) {
	if (!secret || typeof token !== 'string' || !token) return false;
	const value = new URLSearchParams({ secret, response: token, remoteip: ip });
	const response = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
		method: 'POST',
		body: value,
	});
	return response.ok && (await response.json()).success === true;
}

async function findJob(id, env) {
	if (!env.OSHIFIED_JOBS || !ID_PATTERN.test(id)) return null;
	const response = await oshifiedJobs(env).fetch('https://internal/job?id=' + id);
	return response.ok ? response.json() : null;
}

function readAccess(request) {
	return request.headers.get('Authorization')?.replace(/^Bearer /, '') ||
		new URL(request.url).searchParams.get('access');
}

function inputLink(origin, id, slot, access) {
	const url = new URL(`/api/oshified/jobs/${id}/inputs/${slot}`, origin);
	url.searchParams.set('access', access);
	return url.href;
}

function configuredClipAssets(env) {
	return Object.fromEntries([
		['in_end_card', env.OSHIFIED_END_CARD_URL],
		['in_corner_watermark', env.OSHIFIED_CORNER_WATERMARK_URL],
		['in_corner_mask', env.OSHIFIED_CORNER_MASK_URL],
	].map(([name, value]) => [name, validAsset(value)]));
}

async function failJob(jobs, id, attempt, error) {
	await jobs.fetch('https://internal/progress', {
		method: 'POST',
		body: JSON.stringify({ generation_id: id, attempt, eventType: 'error', progressData: { error } }),
	});
}

function mediaLink(origin, id, type, access) {
	const url = new URL(`/api/oshified/jobs/${id}/media/${type}`, origin);
	url.searchParams.set('access', access);
	return url.href;
}

function objectResponse(object, download, rangeRequested = false) {
	if (!object) return new Response(null, { status: 404 });
	const headers = new Headers({
		'Cache-Control': 'private, no-store',
		'X-Content-Type-Options': 'nosniff',
		'Accept-Ranges': 'bytes',
	});
	object.writeHttpMetadata(headers);
	if (download) headers.set('Content-Disposition', `attachment; filename="oshified-${download}"`);
	const { size } = object;
	const range = rangeRequested ? object.range : undefined;
	const start = range?.offset ?? (range?.suffix ? Math.max(0, size - range.suffix) : 0);
	const length = range?.length ?? (range?.suffix ? Math.min(size, range.suffix) : size - start);
	headers.set('Content-Length', String(length));
	if (range) headers.set('Content-Range', `bytes ${start}-${start + length - 1}/${size}`);
	return new Response(object.body, { status: range ? 206 : 200, headers });
}

export function oshifiedRoutes(router) {
	router.post('/api/oshified/jobs', async (request, env) => {
		if (!env.OSHIFIED_JOBS) return reportError('Oshified is not configured', 503);
		const size = Number(request.headers.get('Content-Length'));
		if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_REQUEST) return reportError('Upload is too large or its size is missing', 413);
		const origin = validAsset(env.OSHIFIED_API_ORIGIN);
		const assets = { ...configuredClipAssets(env), in_pose: validAsset(env.OSHIFIED_POSE_URL) };
		if (!origin || Object.values(assets).some((value) => !value)) return reportError('Oshified is not configured', 503);

		const form = await request.formData();
		const method = form.get('mode');
		const variant = form.get('variant');
		const image = form.get('image');
		const audio = form.get('audio');
		const link = form.get('video_url');
		if (!MODES.has(method) || !['female', 'male'].includes(variant)) return reportError('Choose a mode and portrait option', 400);
		if (!(image instanceof File) || !IMAGE_TYPES.has(image.type) || !image.size || image.size > MAX_IMAGE) {
			return reportError('Choose a JPEG, PNG, or WebP image up to 10 MB', 400);
		}
		const hasAudio = audio instanceof File && audio.size > 0;
		if (hasAudio) {
			if (!AUDIO_TYPES.has(audio.type) || audio.size > MAX_AUDIO) return reportError('Choose a supported audio file up to 15 MB', 400);
		} else if (typeof link !== 'string' || !isYoutubeLink(link)) {
			return reportError('Enter a valid YouTube link or upload audio', 400);
		}

		const ip = request.headers.get('CF-Connecting-IP');
		if (!ip || !await checkTurnstile(form.get('turnstileToken'), ip, env.TURNSTILE_SECRET_KEY)) {
			return reportError('Complete the verification challenge', 403);
		}
		if (!env.JWT_SECRET) return reportError('Oshified is not configured', 503);
		const encoder = new TextEncoder();
		const ownerKey = await crypto.subtle.importKey('raw', encoder.encode(env.JWT_SECRET),
			{ name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
		const owner = Array.from(new Uint8Array(await crypto.subtle.sign('HMAC', ownerKey,
			encoder.encode(ip))), (part) => part.toString(16).padStart(2, '0')).join('');
		const id = 'oshified_' + crypto.randomUUID().replaceAll('-', '');
		const access = crypto.randomUUID();
		const inputAccess = crypto.randomUUID();
		const jobs = oshifiedJobs(env);
		const reserve = await jobs.fetch('https://internal/reserve', {
			method: 'POST',
			body: JSON.stringify({ id, owner, access, inputAccess, method, variant, hasAudio, videoUrl: hasAudio ? '' : link }),
		});
		if (reserve.status === 429) return reportError('Daily generation limit reached', 429);
		if (reserve.status === 409) return reportError('A generation is already running', 409);
		if (!reserve.ok) return reportError('Could not start generation', 503);
		try {
			for (const [slot, file] of [['in_image', image], ['in_audio', audio]]) {
				if (!(file instanceof File) || !file.size) continue;
				await env.IMPORTS_BUCKET.put(`oshified/${id}/${slot}`, file, {
					httpMetadata: { contentType: file.type },
				});
				assets[slot] = inputLink(origin, id, slot, inputAccess);
			}
			const result = await runEndpointAsync('basic', {
				generation_id: id,
				user_id: id,
				params: { method, variant, attempt: 1, files: assets, video_url: assets.in_audio ? '' : link },
			}, id, env, { trackLegacyRun: false });
			if (result.error || !result.id) throw new Error('RunPod did not accept the job');
			return jsonResponse({ id, access }, 202);
		} catch {
			await failJob(jobs, id, 1, 'Could not start generation');
			return reportError('Could not start generation', 502);
		}
	});

	router.get('/api/oshified/jobs/:id', async (request, env) => {
		const { id } = request.params;
		const job = await findJob(id, env);
		const access = readAccess(request);
		if (!job || !access || job.access !== access) return new Response(null, { status: 404 });
		const origin = new URL(request.url).origin;
		const snapshot = jsonResponse({
			id, status: job.status, error: job.error, progress: job.progress, canRetryClip: canRetryClip(job),
			artifacts: Object.fromEntries(Object.keys(job.artifacts).map((kind) => [kind, mediaLink(origin, id, kind, access)])),
		});
		snapshot.headers.set('Cache-Control', 'private, no-store');
		return snapshot;
	});

	router.post('/api/oshified/jobs/:id/retry', async (request, env) => {
		const { id } = request.params;
		if (!env.OSHIFIED_JOBS || !ID_PATTERN.test(id)) return new Response(null, { status: 404 });
		const origin = validAsset(env.OSHIFIED_API_ORIGIN);
		const files = configuredClipAssets(env);
		if (!origin || Object.values(files).some((value) => !value)) return reportError('Oshified is not configured', 503);
		const jobs = oshifiedJobs(env);
		const response = await jobs.fetch('https://internal/retry', {
			method: 'POST',
			body: JSON.stringify({ id, access: readAccess(request) }),
		});
		if (!response.ok) return reportError('Animation retry is unavailable', response.status);
		const job = await response.json();
		files.in_portrait = inputLink(origin, id, 'in_portrait', job.inputAccess);
		if (job.hasAudio) files.in_audio = inputLink(origin, id, 'in_audio', job.inputAccess);
		try {
			const result = await runEndpointAsync('basic', {
				generation_id: id,
				user_id: id,
				params: { method: job.method, variant: job.variant, attempt: job.attempt, files, video_url: job.videoUrl, retry_clip: true },
			}, id, env, { trackLegacyRun: false });
			if (result.error || !result.id) throw new Error('RunPod did not accept the retry');
			return jsonResponse({ id }, 202);
		} catch {
			await failJob(jobs, id, job.attempt, 'Could not restart animation');
			return reportError('Could not restart animation', 502);
		}
	});

	router.get('/api/oshified/jobs/:id/inputs/:slot', async (request, env) => {
		const { id, slot } = request.params;
		const job = await findJob(id, env);
		if (!job || job.inputAccess !== readAccess(request)) return new Response(null, { status: 404 });
		if (slot === 'in_portrait' && job.portraitKey) {
			return objectResponse(await env.EXPORTS_BUCKET.get(job.portraitKey));
		}
		if (!['in_image', 'in_audio'].includes(slot)) return new Response(null, { status: 404 });
		return objectResponse(await env.IMPORTS_BUCKET.get(`oshified/${id}/${slot}`));
	});

	router.post('/api/oshified/jobs/:id/share', async (request, env) => {
		const { id } = request.params;
		if (!env.OSHIFIED_JOBS || !ID_PATTERN.test(id)) return new Response(null, { status: 404 });
		const shared = await oshifiedJobs(env).fetch('https://internal/share', {
			method: 'POST',
			body: JSON.stringify({ id, access: readAccess(request) }),
		});
		if (!shared.ok) return new Response(null, { status: 404 });
		const { access } = await shared.json();
		return jsonResponse({ url: mediaLink(new URL(request.url).origin, id, 'clip', access) });
	});

	router.get('/api/oshified/jobs/:id/media/:kind', async (request, env) => {
		const { id, kind } = request.params;
		const job = await findJob(id, env);
		const access = readAccess(request);
		if (!job || !access || !Object.hasOwn(job.artifacts, kind) ||
			(job.access !== access && (kind !== 'clip' || job.shareAccess !== access))) {
			return new Response(null, { status: 404 });
		}
		const download = new URL(request.url).searchParams.get('download') === '1';
		const filename = download ? (kind === 'clip' ? 'clip.mp4' : 'portrait.jpg') : undefined;
		const rangeRequested = request.headers.has('Range');
		const options = rangeRequested ? { range: request.headers } : undefined;
		return objectResponse(await env.EXPORTS_BUCKET.get(job.artifacts[kind], options), filename, rangeRequested);
	});
}
