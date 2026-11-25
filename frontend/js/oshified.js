import { hideModelPreparation, showModelPreparation } from './ui/progress.js';

const api = (import.meta.env.VITE_OSHIFIED_API_ORIGIN || '').replace(/\/$/, '');
const isLoopback = /^https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(api);
const siteKey = import.meta.env.VITE_OSHIFIED_TURNSTILE_SITE_KEY;
const modes = {
    'vtuber-to-real': {
        badge: 'VTuber → IRL',
        imageHint: 'Choose a clear image of the VTuber character.',
        before: 'VTuber',
        after: 'Imagined portrait',
        noteTitle: 'Meet your oshi IRL',
        note: 'This is an imagined portrait of a character. It cannot identify the person behind the avatar.',
    },
    'real-to-vtuber': {
        badge: 'IRL → VTuber',
        imageHint: 'Choose a clear photo of yourself.',
        before: 'Original photo',
        after: 'VTuber portrait',
        noteTitle: 'Join your oshi',
        note: 'Upload a photo to see yourself as a VTuber character.',
    },
};
const mode = new URLSearchParams(location.search).get('mode');
const form = document.querySelector('#generation');
const notice = document.querySelector('#notice');
const status = document.querySelector('#job-status');
const image = document.querySelector('#image');
const audio = document.querySelector('#audio');
const audioPreview = document.querySelector('#audio-preview');
const link = document.querySelector('#video-url');
const submit = document.querySelector('#submit');
const next = document.querySelector('#next');
const previous = document.querySelector('#previous');
const retry = document.querySelector('#retry-clip');
const startOver = document.querySelector('#start-over');
const share = document.querySelector('#share');
const result = document.querySelector('#results');
const steps = [...form.querySelectorAll('fieldset')];
const stateKey = `oshified-job:${api}:${mode}`;
const clipSeconds = 20;
const mediaLimits = { image: 10 * 1024 ** 2, audio: 15 * 1024 ** 2 };
const previewUrls = new Map();
let step = 0;
let challenge;
let job;
let pollTimer;

function configuredUrl(value, base) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
        const url = new URL(value, base);
        if (url.username || url.password) return null;
        if (url.protocol === 'https:' ||
            (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
            return url;
        }
    } catch {
        // Ignore malformed operator configuration.
    }
    return null;
}

function loadConfiguredPresets() {
    try {
        const records = JSON.parse(import.meta.env.VITE_OSHIFIED_PRESETS_JSON || '[]');
        if (Array.isArray(records)) {
            return records.filter(record => typeof record?.id === 'string' &&
                record.id.trim() && typeof record.name === 'string' && record.name.trim());
        }
    } catch {
        console.warn('Could not read Oshified examples from VITE_OSHIFIED_PRESETS_JSON.');
    }
    return [];
}

function isYoutube(url) {
    return url.protocol === 'https:' &&
        ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'].includes(url.hostname);
}

async function choosePreset(kind, url, button) {
    const input = kind === 'image' ? image : audio;
    if (kind === 'audio' && isYoutube(url)) {
        link.value = url.href;
        link.dispatchEvent(new Event('input'));
        message();
        return;
    }
    const previousFile = input.files[0];
    const previousLink = link.value;
    const currentStep = step;
    const container = button.parentElement;
    const buttons = [...container.querySelectorAll('button')];
    buttons.forEach(item => { item.disabled = true; });
    next.disabled = true;
    container.setAttribute('aria-busy', 'true');
    message();
    try {
        const response = await fetch(url, { credentials: 'omit' });
        if (!response.ok) throw new Error();
        const blob = await response.blob();
        if (!blob.size || blob.size > mediaLimits[kind] || !input.accept.split(',').includes(blob.type)) {
            throw new Error();
        }
        if (step !== currentStep || input.files[0] !== previousFile ||
            (kind === 'audio' && link.value !== previousLink)) return;
        const fileName = url.pathname.split('/').pop() || ('oshified-example-' + kind);
        const files = new DataTransfer();
        files.items.add(new File([blob], fileName, { type: blob.type }));
        input.files = files.files;
        input.dispatchEvent(new Event('change'));
    } catch {
        if (step === currentStep) message('Could not load this example. Choose another example or upload a file.');
    } finally {
        container.removeAttribute('aria-busy');
        buttons.forEach(item => { item.disabled = false; });
        next.disabled = false;
    }
}

function renderPresets(kind, records) {
    const container = document.querySelector('#' + kind + '-presets');
    for (const record of records) {
        const asset = kind === 'image' ? record.imageUrl : record.audioUrl || record.previewAudio;
        const url = configuredUrl(asset, location.origin);
        if (!url) continue;
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = record.name.trim();
        button.addEventListener('click', () => choosePreset(kind, url, button));
        container.append(button);
    }
    if (container.childElementCount) {
        const label = document.createElement('span');
        label.textContent = kind === 'image' ? 'Or choose an image example' : 'Or choose an audio example';
        container.prepend(label);
        container.hidden = false;
    }
}

function message(value = '') {
    if (notice.textContent === value) return;
    notice.textContent = value;
    notice.hidden = !value;
}

function writeStatus(value) {
    if (status.textContent !== value) status.textContent = value;
}

function showStep(index, focus = false) {
    step = index;
    steps.forEach((fieldset, position) => { fieldset.hidden = position !== step; });
    document.querySelector('#step-position').textContent = `Step ${step + 1} of ${steps.length}`;
    previous.hidden = step === 0;
    next.hidden = step === steps.length - 1;
    submit.hidden = !next.hidden;
    if (!submit.hidden) loadChallenge();
    if (focus) {
        const title = steps[step].querySelector('legend');
        title.tabIndex = -1;
        title.focus();
    }
}

function preview(input, element, selection) {
    const oldUrl = previewUrls.get(element);
    if (oldUrl) URL.revokeObjectURL(oldUrl);
    const file = input.files[0];
    element.hidden = !file;
    selection.hidden = !file;
    if (file) {
        const url = URL.createObjectURL(file);
        previewUrls.set(element, url);
        element.src = url;
    } else {
        element.removeAttribute('src');
        previewUrls.delete(element);
    }
}

function updateAudio() {
    preview(audio, audioPreview, document.querySelector('#audio-selection'));
    if (audio.files.length) link.value = '';
}

function advance() {
    if (next.disabled) return;
    if (step === 0) {
        image.setCustomValidity(image.files[0]?.size > mediaLimits.image ? 'Choose an image up to 10 MB.' : '');
        if (!image.reportValidity()) return;
    }
    if (step === 1) {
        audio.setCustomValidity(audio.files[0]?.size > mediaLimits.audio ? 'Choose an audio file up to 15 MB.' : '');
        if (!audio.reportValidity()) return;
        if (!audio.files.length && !link.value) {
            message('Choose audio or enter a link.');
            link.focus();
            return;
        }
        if (!audio.files.length && !link.reportValidity()) return;
    }
    message();
    showStep(step + 1, true);
}

function loadChallenge() {
    if (isLoopback || challenge !== undefined || !siteKey) return;
    const render = () => {
        if (challenge === undefined && window.turnstile) {
            challenge = window.turnstile.render('#challenge', { sitekey: siteKey });
        }
    };
    if (window.turnstile) {
        render();
    } else if (!document.querySelector('#turnstile-script')) {
        const script = document.createElement('script');
        script.id = 'turnstile-script';
        script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
        script.addEventListener('load', render);
        script.addEventListener('error', () => {
            script.remove();
            message('Could not load verification. Go back one step and try again.');
        });
        document.head.append(script);
    }
}

function sessionHeaders() {
    return { Authorization: `Bearer ${job.access}` };
}

function setArtifact(kind, url) {
    if (!url) return;
    const panel = document.querySelector(`#${kind}-result`);
    const media = document.querySelector(`#${kind}`);
    const download = new URL(url, api || location.origin);
    if (media.src !== download.href) media.src = download.href;
    download.searchParams.set('download', '1');
    document.querySelector(`#download-${kind}`).href = download.href;
    panel.hidden = false;
    result.hidden = false;
    if (kind === 'still') {
        const before = document.querySelector('#before');
        const original = previewUrls.get(document.querySelector('#image-preview'));
        if (original) {
            before.src = original;
            document.querySelector('#before-result').hidden = false;
        }
    }
}

function renderJob(data) {
    form.hidden = true;
    status.hidden = false;
    const phase = data.progress?.phase;
    const terminal = data.status === 'failed' || data.status === 'complete';
    if (['starting', 'downloading', 'loading'].includes(phase) && !terminal) {
        showModelPreparation(data.progress);
    } else {
        hideModelPreparation();
    }

    const stages = { portrait: 'Making your portrait', still: 'Preparing the portrait', clip: 'Animating your clip' };
    if (data.status === 'complete') {
        writeStatus('Your Oshified transformation is ready.');
    } else if (data.status === 'failed') {
        writeStatus(data.error || 'Generation failed.');
    } else if (['starting', 'downloading', 'loading'].includes(phase)) {
        status.hidden = true;
    } else {
        writeStatus(stages[data.progress?.stageName] || 'Preparing your generation.');
    }

    setArtifact('still', data.artifacts?.still);
    setArtifact('clip', data.artifacts?.clip);
    retry.hidden = !(data.status === 'failed' && data.canRetryClip === true);
    startOver.hidden = !terminal;
}

async function poll() {
    try {
        const response = await fetch(`${api}/api/oshified/jobs/${job.id}`, { headers: sessionHeaders() });
        if (response.status === 404) {
            sessionStorage.removeItem(stateKey);
            hideModelPreparation();
            startOver.hidden = false;
            return message('This previous job is no longer available.');
        }
        if (!response.ok) throw new Error('Could not retrieve progress.');
        const data = await response.json();
        renderJob(data);
        message();
        if (['complete', 'failed'].includes(data.status)) return;
    } catch {
        message('Reconnecting to your generation…');
    }
    pollTimer = window.setTimeout(poll, 2000);
}

async function submitJob(event) {
    event.preventDefault();
    if (step < steps.length - 1) return advance();
    if (!api || (!isLoopback && !siteKey)) return message('The worker must be configured before generating.');
    const token = challenge === undefined ? '' : window.turnstile.getResponse(challenge);
    if (!isLoopback && !token) return message('Complete the verification challenge.');
    message();
    submit.disabled = true;
    try {
        const body = new FormData(form);
        body.set('mode', mode);
        if (token) body.set('turnstileToken', token);
        const response = await fetch(`${api}/api/oshified/jobs`, { method: 'POST', body });
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || 'Could not start generation.');
        job = { ...data, mode };
        try {
            sessionStorage.setItem(stateKey, JSON.stringify(job));
        } catch {
            // Continue polling when browser storage is unavailable.
        }
        renderJob({ status: 'queued', progress: { phase: 'starting' } });
        await poll();
    } catch (error) {
        message(error.message);
        if (challenge !== undefined) window.turnstile.reset(challenge);
    } finally {
        submit.disabled = false;
    }
}

function showModeNote(config) {
    const noteKey = `oshified-mode-note:${mode}`;
    if (sessionStorage.getItem(noteKey)) return;
    const note = document.querySelector('#mode-note');
    document.querySelector('#mode-note-title').textContent = config.noteTitle;
    document.querySelector('#mode-note-description').textContent = config.note;
    note.addEventListener('close', () => sessionStorage.setItem(noteKey, 'seen'), { once: true });
    note.showModal();
}

async function shareClip() {
    share.disabled = true;
    try {
        const response = await fetch(`${api}/api/oshified/jobs/${job.id}/share`, {
            method: 'POST', headers: sessionHeaders(),
        });
        if (!response.ok) throw new Error('Share link unavailable.');
        const { url } = await response.json();
        if (navigator.share) {
            try {
                await navigator.share({ title: 'Oshified', url });
                message();
                return;
            } catch (error) {
                if (error.name === 'AbortError') return;
            }
        }
        try {
            await navigator.clipboard.writeText(url);
            writeStatus('Share link copied.');
        } catch {
            const field = document.querySelector('#share-link');
            field.value = url;
            document.querySelector('#share-fallback').hidden = false;
            field.focus();
            field.select();
        }
    } catch (error) {
        message(error.message);
    } finally {
        share.disabled = false;
    }
}

async function retryClip() {
    retry.disabled = true;
    try {
        const response = await fetch(`${api}/api/oshified/jobs/${job.id}/retry`, {
            method: 'POST', headers: sessionHeaders(),
        });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.message || 'Could not retry animation.');
        }
        message();
        renderJob({ status: 'queued', progress: { phase: 'starting' } });
        await poll();
    } catch (error) {
        message(error.message);
    } finally {
        retry.disabled = false;
    }
}

const sourceUrl = configuredUrl(import.meta.env.VITE_OSHIFIED_SOURCE_URL);
if (sourceUrl) {
    const source = document.querySelector('#source-code');
    source.href = sourceUrl.href;
    source.parentElement.hidden = false;
}

if (Object.hasOwn(modes, mode)) {
    document.body.classList.remove('is-landing');
    share.hidden = isLoopback;
    const config = modes[mode];
    const badge = document.querySelector('#mode-badge');
    badge.textContent = config.badge;
    badge.hidden = false;
    document.querySelector('#back-home').hidden = false;
    document.querySelector('#image-description').textContent = config.imageHint;
    document.querySelector('#before-label').textContent = config.before;
    document.querySelector('#still-label').textContent = config.after;
    document.querySelector('#modes').hidden = true;
    form.hidden = false;
    form.noValidate = true;
    const imagePreview = document.querySelector('#image-preview');
    const imageSelection = document.querySelector('#image-selection');
    image.addEventListener('change', () => preview(image, imagePreview, imageSelection));
    audio.addEventListener('change', updateAudio);
    link.addEventListener('input', () => {
        if (link.value && audio.files.length) {
            audio.value = '';
            updateAudio();
        }
    });
    if (mode === 'vtuber-to-real') {
        const presets = loadConfiguredPresets();
        renderPresets('image', presets);
        renderPresets('audio', presets);
    }
    document.querySelector('#clear-image').addEventListener('click', () => {
        image.value = '';
        preview(image, imagePreview, imageSelection);
        image.focus();
    });
    document.querySelector('#clear-audio').addEventListener('click', () => {
        audio.value = '';
        updateAudio();
        audio.focus();
    });
    audioPreview.addEventListener('timeupdate', () => {
        const end = Math.min(audioPreview.duration, clipSeconds);
        if (audioPreview.currentTime >= end && !audioPreview.paused) audioPreview.pause();
    });
    audioPreview.addEventListener('play', () => {
        if (audioPreview.currentTime >= clipSeconds) audioPreview.currentTime = 0;
    });
    audioPreview.addEventListener('seeking', () => {
        if (audioPreview.currentTime > clipSeconds) audioPreview.currentTime = clipSeconds;
    });
    next.addEventListener('click', advance);
    previous.addEventListener('click', () => {
        message();
        showStep(step - 1, true);
    });
    form.addEventListener('submit', submitJob);
    startOver.addEventListener('click', () => {
        window.clearTimeout(pollTimer);
        sessionStorage.removeItem(stateKey);
        location.reload();
    });
    share.addEventListener('click', shareClip);
    retry.addEventListener('click', retryClip);
    showStep(0);
    try {
        const saved = JSON.parse(sessionStorage.getItem(stateKey));
        if (saved?.id && saved?.access && saved.mode === mode && api) {
            job = saved;
            renderJob({ status: 'queued', progress: { phase: 'starting' } });
            poll();
        } else {
            showModeNote(config);
        }
    } catch {
        // Reading storage is optional.
    }
}
