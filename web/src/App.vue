<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
interface Job {
  id: string;
  name: string;
  status: string;
  stage: string;
  progress: number;
  original_ready: boolean;
  error?: string;
  completed_frames?: number;
  total_frames?: number;
  elapsed_seconds?: number;
  started_at?: number;
  metadata?: {
    width: number;
    height: number;
    fps: number;
    frames: number;
    duration: number;
    size?: number;
    device?: string;
  };
}
const job = ref<Job>();
const history = ref<Job[]>([]);
const clearingHistory = ref(false);
const historyMessage = ref('');
async function clearHistory() {
  if (busy.value || clearingHistory.value || !history.value.length) return;
  if (!window.confirm('清空最近的处理任务？只清除历史列表记录，不删除原视频、结果或遮罩文件。当前预览仍会保留。')) return;
  clearingHistory.value = true;
  historyMessage.value = '';
  try {
    const value = await api('/api/history/clear', { method: 'POST' });
    await refreshHistory();
    historyMessage.value = `已清空 ${value.cleared} 条记录，原视频和处理结果仍保留在本机。`;
  } catch (e) { error.value = String(e); }
  finally { clearingHistory.value = false; }
}
const health = ref<{ ready: boolean; device: string }>();
const original = ref<HTMLVideoElement>();
const result = ref<HTMLVideoElement>();
const input = ref<HTMLInputElement>();
const comparison = ref<HTMLElement>();
const originalViewport = ref<HTMLElement>();
const resultViewport = ref<HTMLElement>();
const videoBounds = ref({ width: 0, height: 0 });
let viewportObserver: ResizeObserver | undefined;
function updateVideoBounds() {
  const viewport = resultViewport.value;
  const width = original.value?.videoWidth || result.value?.videoWidth || job.value?.metadata?.width || 0;
  const height = original.value?.videoHeight || result.value?.videoHeight || job.value?.metadata?.height || 0;
  if (!viewport || !width || !height) { videoBounds.value = { width: 0, height: 0 }; return; }
  const scale = Math.min(viewport.clientWidth / width, viewport.clientHeight / height);
  videoBounds.value = { width: width * scale, height: height * scale };
}
const looping = ref(true);
const fullscreenElement = ref<Element | null>(null);
const playbackRate = ref(1);
const scrubbing = ref(false);
let resumeAfterScrub = false;
let pendingSeek: number | null = null;
function beginScrub() {
  if (scrubbing.value) return;
  resumeAfterScrub = playing.value;
  scrubbing.value = true;
  pause();
}
function endScrub() {
  if (!scrubbing.value) return;
  scrubbing.value = false;
  if (pendingSeek === null && resumeAfterScrub) { resumeAfterScrub = false; togglePlay(); }
}
function seekComplete() {
  if (pendingSeek === null || !original.value || Math.abs(original.value.currentTime - pendingSeek) > .06) return;
  pendingSeek = null;
  if (!scrubbing.value && resumeAfterScrub) { resumeAfterScrub = false; togglePlay(); }
}
const speedOptions = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2];
function updateFullscreen() { fullscreenElement.value = document.fullscreenElement; }
function applyPlaybackRate() {
  if (original.value) original.value.playbackRate = playbackRate.value;
  if (result.value) result.value.playbackRate = playbackRate.value;
}
const dragging = ref(false);
const customColor = ref("#73978c");
const uploadProgress = ref(0);
const uploadName = ref('');
const uploadBytes = ref(0);
const uploadTotal = ref(0);
const megabytes = (value: number) => `${(value / 1024 / 1024).toFixed(1)} MB`;
const exportMode = ref("transparent");
const exportPending = ref(false);
const mattePreview = ref("");
const exportDownload = ref("");
const now = ref(Date.now());
let clockTimer: ReturnType<typeof setInterval> | undefined;
let exportVersion = 0;
const remaining = computed(() => {
  if (job.value?.status === 'queued') return '排队中，开始后估算';
  if (job.value?.status !== 'processing') return '';
  const count = job.value.completed_frames ?? 0;
  const total = job.value.total_frames ?? job.value.metadata?.frames ?? 0;
  if (count < 3 || !job.value.started_at || !total) return '预计剩余时间：估算中';
  const elapsed = now.value / 1000 - job.value.started_at;
  const seconds = Math.max(1, elapsed / count * (total - count) + elapsed * .08);
  return `预计剩余：约 ${seconds < 60 ? Math.ceil(seconds) + ' 秒' : Math.ceil(seconds / 60) + ' 分钟'}（动态估算）`;
});
async function prepareExport() {
  const version = ++exportVersion;
  mattePreview.value = '';
  exportDownload.value = '';
  if (job.value?.status !== 'done') { exportPending.value = false; return; }
  if (exportMode.value === 'transparent') { exportPending.value = false; exportDownload.value = `/api/jobs/${job.value.id}/download`; return; }
  exportPending.value = true;
  try {
    const value = await api(`/api/jobs/${job.value.id}/export`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: exportMode.value, color: customColor.value }) });
    if (version !== exportVersion) return;
    exportDownload.value = value.download;
    if (exportMode.value === 'matte') mattePreview.value = value.preview;
  } catch (e) { if (version === exportVersion) error.value = String(e); }
  finally { if (version === exportVersion) exportPending.value = false; }
}
const phases = ["上传素材", "解码与预览", "加载模型", "识别与预热", "逐帧抠像", "透明合成"];
const phaseIndex = computed(() => uploading.value ? 0 : percent.value < 12 ? 1 : percent.value < 20 ? 2 : percent.value < 25 ? 3 : percent.value < 88 ? 4 : 5);
function phasePercent(index: number) {
  if (index < phaseIndex.value) return 100;
  if (index > phaseIndex.value || job.value?.status === 'queued') return 0;
  const starts = [0, 0, 12, 20, 25, 88];
  const ends = [100, 12, 20, 25, 88, 100];
  if (index === 0) return uploadProgress.value;
  if (index === 4 && job.value?.total_frames) return Math.round((job.value.completed_frames ?? 0) / job.value.total_frames * 100);
  return Math.min(100, Math.max(0, Math.round((percent.value - starts[index]) / (ends[index] - starts[index]) * 100)));
}
function phaseDetail(index: number) {
  if (index === 4) return `${job.value?.completed_frames ?? 0}/${job.value?.total_frames ?? job.value?.metadata?.frames ?? '—'} 张 · ${phasePercent(index)}%`;
  if (index === 3 && index === phaseIndex.value && job.value?.stage.includes('预热')) return `${job.value.stage} · ${phasePercent(index)}%`;
  return `${phasePercent(index)}%`;
}
function chooseFile() { if (!busy.value) input.value?.click(); }
function dropFile(event: DragEvent) { dragging.value = false; upload(event.dataTransfer?.files[0]); }
function clearMaterial() {
  if (busy.value) return;
  clearTimeout(pollTimer);
  resetSource();
  job.value = undefined;
  error.value = "";
  exportMode.value = 'transparent';
  background.value = 'checker';
}
async function fullscreen(element?: HTMLElement) {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await element?.requestFullscreen();
    updateFullscreen();
  } catch { error.value = "当前浏览器未允许全屏，请检查浏览器设置。"; }
}
function keyboard(event: KeyboardEvent) {
  const target = event.target as HTMLElement;
  if (event.code !== "Space" || target?.closest("input:not([type=range]), textarea, select, [contenteditable]")) return;
  event.preventDefault();
  event.stopPropagation();
  // 全屏按钮仍可能拥有焦点，拦截空格默认的按钮激活行为。
  if (event.type === 'keydown' && !event.repeat && duration.value) togglePlay();
}
function uploadRequest(form: FormData): Promise<Job> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/jobs");
    request.upload.onprogress = event => {
      uploadBytes.value = event.loaded;
      if (event.lengthComputable) { uploadTotal.value = event.total; uploadProgress.value = Math.round(event.loaded / event.total * 100); }
    };
    request.onerror = () => reject(Error("上传连接中断，请确认服务仍在运行。"));
    request.onload = () => {
      try { const value = JSON.parse(request.responseText); request.status >= 200 && request.status < 300 ? resolve(value) : reject(Error(value.detail ?? "上传失败")); }
      catch { reject(Error("上传响应无效")); }
    };
    request.send(form);
  });
}
const uploading = ref(false);
const error = ref("");
const localSource = ref("");
const background = ref("checker");
const playing = ref(false);
let playbackVersion = 0;
const time = ref(0);
const duration = ref(0);
const originalUnsupported = ref(false);
const originalSize = ref("");
function chooseBackground(id: string) {
  if (exportMode.value === 'matte') return;
  if (exportMode.value === 'custom') {
    if (id === 'checker') return;
    if (id === 'black') customColor.value = '#17191d';
    if (id === 'white') customColor.value = '#ffffff';
    if (id === 'green') customColor.value = '#00b140';
    background.value = 'custom';
  } else background.value = id;
}
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
const busy = computed(
  () =>
    uploading.value ||
    ["queued", "processing", "uploading"].includes(job.value?.status ?? ""),
);
const originalSrc = computed(() =>
  job.value?.original_ready
    ? `/api/jobs/${job.value.id}/original`
    : localSource.value,
);
const resultSrc = computed(() =>
  job.value?.status === "done" ? exportMode.value === 'matte' ? mattePreview.value : `/api/jobs/${job.value.id}/result` : "",
);
const exportLabel = computed(() => exportMode.value === 'transparent' ? '透明 WebM' : exportMode.value === 'matte' ? 'Alpha Matte MP4' : '自定义背景 MP4');
const percent = computed(() => Math.round(job.value?.progress ?? 0));
const label = computed(
  () =>
    ({
      done: "已完成",
      failed: "失败",
      cancelled: "已取消",
      queued: "排队中",
      processing: "处理中",
    })[job.value?.status ?? ""] ?? "等待上传",
);
const fmt = (seconds: number) =>
  `${Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(seconds % 60)
    .toString()
    .padStart(2, "0")}`;
async function api(path: string, options?: RequestInit) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw Error(value.detail ?? "请求失败");
  return value;
}
async function refreshHistory() {
  try {
    history.value = await api("/api/jobs");
  } catch {
    /* 正在重启时保留当前历史，不覆盖任务错误。 */
  }
}
async function poll(id: string) {
  try {
    const value = await api(`/api/jobs/${id}`);
    if (disposed || job.value?.id !== id) return;
    job.value = value;
    if (["queued", "processing"].includes(value.status))
      pollTimer = setTimeout(() => poll(id), 1000);
    else await refreshHistory();
  } catch (e) {
    if (!disposed)
      error.value = `连接中断，请确认本地服务仍在运行：${String(e)}`;
  }
}
function pause() {
  playbackVersion++;
  original.value?.pause();
  result.value?.pause();
  playing.value = false;
}
function resetSource() {
  pendingSeek = null;
  scrubbing.value = false;
  resumeAfterScrub = false;
  if (localSource.value) URL.revokeObjectURL(localSource.value);
  localSource.value = "";
  pause();
  time.value = 0;
  duration.value = 0;
  originalUnsupported.value = false;
  originalSize.value = '';
}
async function upload(file?: File) {
  if (!file || busy.value) return;
  clearTimeout(pollTimer);
  resetSource();
  error.value = "";
  job.value = undefined;
  exportMode.value = 'transparent';
  background.value = 'checker';
  localSource.value = URL.createObjectURL(file);
  uploading.value = true;
  uploadProgress.value = 0;
  uploadName.value = file.name;
  uploadBytes.value = 0;
  uploadTotal.value = 0;
  await nextTick();
  try {
    const form = new FormData();
    form.append("file", file);
    job.value = await uploadRequest(form);
    await refreshHistory();
    await poll(job.value!.id);
  } catch (e) {
    error.value = String(e);
  } finally {
    uploading.value = false;
    if (input.value) input.value.value = "";
  }
}
async function selectJob(item: Job) {
  if (busy.value && job.value?.id !== item.id) return;
  clearTimeout(pollTimer);
  resetSource();
  error.value = "";
  job.value = item;
  await poll(item.id);
}
async function togglePlay() {
  if (playing.value) {
    pause();
    return;
  }
  if (!original.value) return;
  const version = ++playbackVersion;
  playing.value = true;
  try {
    if (resultSrc.value && result.value) {
      result.value.currentTime = original.value.currentTime;
      await Promise.all([original.value.play(), result.value.play()]);
    } else await original.value.play();
  } catch {
    if (version !== playbackVersion) return;
    pause();
    error.value = "视频尚未就绪或无法播放，请等待原片预览生成后重试。";
  }
}
function seek(value: number) {
  const target = Math.max(0, Math.min(value, duration.value || value));
  time.value = target;
  pendingSeek = target;
  if (original.value) original.value.currentTime = target;
  if (result.value && resultSrc.value) result.value.currentTime = target;
}
function sync() {
  if (!original.value || scrubbing.value || pendingSeek !== null) return;
  time.value = original.value.currentTime;
  if (
    result.value &&
    resultSrc.value &&
    Math.abs(result.value.currentTime - time.value) > 0.08
  )
    result.value.currentTime = time.value;
}
async function cancel() {
  if (job.value) {
    try {
      await api(`/api/jobs/${job.value.id}/cancel`, { method: "POST" });
    } catch (e) {
      error.value = String(e);
    }
  }
}
watch([originalSrc, resultSrc], async () => {
  pause();
  await nextTick();
  original.value?.load();
  result.value?.load();
  updateVideoBounds();
});
watch([exportMode, customColor, () => job.value?.id, () => job.value?.status], prepareExport);
watch(exportMode, mode => { background.value = mode === 'custom' ? 'custom' : mode === 'matte' ? 'black' : 'checker'; });
watch(playbackRate, applyPlaybackRate);
onMounted(async () => {
  viewportObserver = new ResizeObserver(updateVideoBounds);
  if (resultViewport.value) viewportObserver.observe(resultViewport.value);
  window.addEventListener('pointerup', endScrub);
  window.addEventListener('pointercancel', endScrub);
  document.addEventListener('fullscreenchange', updateFullscreen);
  clockTimer = setInterval(() => now.value = Date.now(), 1000);
  window.addEventListener("keydown", keyboard, true);
  window.addEventListener("keyup", keyboard, true);
  try {
    health.value = await api("/api/health");
    await refreshHistory();
  } catch {
    error.value = "无法连接本地服务，请通过一键启动.cmd打开。";
  }
});
onBeforeUnmount(() => {
  viewportObserver?.disconnect();
  window.removeEventListener('pointerup', endScrub);
  window.removeEventListener('pointercancel', endScrub);
  document.removeEventListener('fullscreenchange', updateFullscreen);
  clearInterval(clockTimer);
  window.removeEventListener("keydown", keyboard, true);
  window.removeEventListener("keyup", keyboard, true);
  disposed = true;
  clearTimeout(pollTimer);
  resetSource();
});
</script>

<template>
  <main>
    <header class="topbar">
      <a class="brand" href="/"><img src="/logo-v1.png" alt="VideoMatting 标志" /><small>清影工坊 · 视频透明背景<em>VideoMatting</em></small></a
      ><span class="device"
        ><i></i>{{ health?.device ?? "正在连接" }} · 本地处理</span
      >
    </header>
    <section class="intro">
      <div>
        <p class="eyebrow">DIGITAL HUMAN / VIDEO WORKSPACE</p>
        <h1>让角色，走出背景。</h1>
        <p class="subtitle">
          免费的本地视频背景移除工具。上传单人物视频，自动精细抠像；无需按次付费，视频不上传云端。
        </p>
      </div>
      <div class="upload-area">
        <input
          ref="input"
          type="file"
          accept="video/mp4,video/quicktime,video/webm,video/x-matroska"
          :disabled="busy"
          @change="upload(($event.target as HTMLInputElement).files?.[0])"
        /><small>单人物 · 支持 4K（4096×2160） · 最长 60 秒 · 最大 250 MB</small>
      </div>
    </section>
    <p v-if="health && !health.ready" class="notice">
      模型尚未准备完成。请先运行安装环境.cmd，下载完成后重新启动；视频不会上传到云端。
    </p>
    <p v-if="error || job?.error" class="error" role="alert">
      {{ error || job?.error }}
    </p>
    <section class="workspace">
      <div class="workspace-head">
        <div>
          <strong>{{ job?.name ?? "视频对比预览" }}</strong
          ><span class="badge" :class="job?.status">{{ label }}</span>
        </div>
      </div>
      <div class="preview-controls">
        <div class="export-modes"><span>预览与导出模式</span><button v-for="[id, title] in [['transparent', '透明 WebM'], ['custom', '自定义背景'], ['matte', 'Alpha Matte']]" :key="id" :aria-pressed="exportMode === id" @click="exportMode = id">{{ title }}</button></div>
        <div v-if="exportMode !== 'matte'" class="backgrounds" aria-label="结果预览背景">
          <button
            v-for="[id, text] in [
              ['checker', '棋盘格'],
              ['black', '黑底'],
              ['white', '白底'],
              ['green', '绿幕'],
              ['custom', '自定义'],
            ]"
            :key="id"
            :aria-pressed="background === id"
            :disabled="exportMode === 'matte' || (exportMode === 'custom' && id === 'checker')"
            @click="chooseBackground(id)"
          >
            {{ text }}
          </button>
          <input type="color" v-model="customColor" :disabled="exportMode === 'matte'" aria-label="自定义预览颜色" @input="background = 'custom'" />
        </div>
      </div>
      <p class="preview-note">{{ exportPending ? '正在准备对应格式的预览与下载…' : exportMode === 'custom' ? '预览颜色将写入导出的 MP4，可使用同一行的颜色选择器调整。' : exportMode === 'matte' ? '白色保留、黑色移除、灰色半透明；导出黑白遮罩 MP4。' : '棋盘格及底色仅用于预览，下载的 WebM 保留透明通道。' }}</p>
      <div v-if="uploading" class="upload-progress" role="status" aria-live="polite">
        <div><strong>{{ uploadProgress === 100 ? '上传完成，正在确认…' : '正在上传视频…' }}</strong><span>{{ uploadProgress }}%</span></div>
        <progress max="100" :value="uploadTotal ? uploadProgress : undefined" aria-label="视频上传进度"></progress>
        <small>{{ uploadName }} · {{ megabytes(uploadBytes) }}{{ uploadTotal ? ` / ${megabytes(uploadTotal)}` : ' · 等待传输进度' }} · 仅传输到本机服务</small>
      </div>
      <div ref="comparison" class="comparison">
      <div class="players">
        <article>
          <div class="player-label">
            <span>01 / 原视频 <small>{{ originalSize || (job?.metadata ? `${job.metadata.width} × ${job.metadata.height}` : '') }}</small></span><button class="remove-material" :disabled="busy || !originalSrc" @click="clearMaterial">删除素材 / 重新制作</button>
          </div>
          <div ref="originalViewport" class="viewport original" :class="{ dragging }" @click="!originalSrc && chooseFile()" @dragover.prevent="dragging = !busy" @dragleave.prevent="dragging = false" @drop.prevent="dropFile">
            <video
              v-if="originalSrc"
              ref="original"
              :src="originalSrc"
              muted
              :loop="looping"
              playsinline
              preload="metadata"
              @loadedmetadata="duration = original?.duration ?? 0; originalSize = `${original?.videoWidth} × ${original?.videoHeight}`; applyPlaybackRate(); updateVideoBounds()"
              @click.stop="togglePlay"
              @timeupdate="sync"
              @seeking="sync"
              @seeked="seekComplete"
              @ended="!looping && pause()"
              @error="originalUnsupported = true"
            ></video>
            <div v-else class="empty">
              <span class="empty-icon">↥</span>
              <h3>从原视频开始</h3>
              <p>点击上传，或将视频拖到上传区域</p>
            </div>
            <button v-if="!fullscreenElement" class="primary upload-overlay" :class="{ 'has-video': originalSrc }" :disabled="busy" @click.stop="chooseFile">{{ uploading ? "正在上传…" : busy ? "视频处理中…" : originalSrc ? "＋ 更换原视频" : "＋ 上传原视频" }}</button>
            <div v-if="originalSrc && fullscreenElement !== comparison" class="viewport-tools"><button v-if="fullscreenElement === originalViewport" class="playback-icon" :aria-label="playing ? '暂停' : '播放'" :title="playing ? '暂停' : '播放'" @click.stop="togglePlay"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path v-if="playing" d="M6 4h4v16H6zm8 0h4v16h-4z"/><path v-else d="M7 4v16l13-8z"/></svg></button><label v-if="fullscreenElement === originalViewport" class="speed-control" @click.stop><select v-model.number="playbackRate" aria-label="原视频全屏播放速度"><option v-for="speed in speedOptions" :key="speed" :value="speed">{{ speed }}×</option></select></label><button class="fullscreen-icon" :aria-label="fullscreenElement === originalViewport ? '关闭全屏' : '原视频全屏'" :title="fullscreenElement === originalViewport ? '关闭' : '全屏'" @click.stop="fullscreen(originalViewport)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path v-if="fullscreenElement === originalViewport" d="M6 6l12 12M18 6L6 18"/><path v-else d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/></svg></button></div>
            <p
              v-if="originalUnsupported && !job?.original_ready"
              class="codec-note"
            >
              正在准备兼容浏览器的原片预览…
            </p>
          </div>
        </article>
        <article>
          <div class="player-label">
            <span>02 / {{ exportMode === 'matte' ? 'Alpha Matte' : exportMode === 'custom' ? '背景合成结果' : '透明结果' }} <small>{{ job?.metadata ? `${job.metadata.width} × ${job.metadata.height}` : '' }}</small></span><small>{{ exportLabel }}</small>
          </div>
          <div ref="resultViewport" class="viewport result-viewport">
            <div v-if="resultSrc && videoBounds.width" class="video-background" :class="background" :style="{ width: `${videoBounds.width}px`, height: `${videoBounds.height}px`, ...(background === 'custom' ? { backgroundColor: customColor } : {}) }" aria-hidden="true"></div>
            <video
              v-if="resultSrc"
              ref="result"
              :src="resultSrc"
              muted
              :loop="looping"
              playsinline
              preload="auto"
              @loadedmetadata="applyPlaybackRate(); updateVideoBounds()"
              @click.stop="togglePlay"
            ></video>
            <div v-else class="empty">
              <span class="empty-icon">◌</span>
              <h3>{{ busy ? "正在分离人物与背景" : "等待处理结果" }}</h3>
              <p>
                {{
                  busy
                    ? (job?.stage ?? "上传视频后自动开始")
                    : "透明区域将显示选定的预览背景"
                }}
              </p>
            </div>
            <div v-if="resultSrc && fullscreenElement !== comparison" class="viewport-tools"><button v-if="fullscreenElement === resultViewport" class="playback-icon" :aria-label="playing ? '暂停' : '播放'" :title="playing ? '暂停' : '播放'" @click="togglePlay"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path v-if="playing" d="M6 4h4v16H6zm8 0h4v16h-4z"/><path v-else d="M7 4v16l13-8z"/></svg></button><label v-if="fullscreenElement === resultViewport" class="speed-control"><select v-model.number="playbackRate" aria-label="结果全屏播放速度"><option v-for="speed in speedOptions" :key="speed" :value="speed">{{ speed }}×</option></select></label><button class="fullscreen-icon" :aria-label="fullscreenElement === resultViewport ? '关闭全屏' : '结果全屏'" :title="fullscreenElement === resultViewport ? '关闭' : '全屏'" @click="fullscreen(resultViewport)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path v-if="fullscreenElement === resultViewport" d="M6 6l12 12M18 6L6 18"/><path v-else d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/></svg></button></div>
          </div>
        </article>
      </div>
      <div class="transport">
        <button :disabled="!originalSrc || !duration" @click="togglePlay">
          {{ playing ? "Ⅱ 暂停" : "▶ 同步播放" }}</button
        ><span>{{ fmt(time) }}</span
        ><input
          type="range"
          min="0"
          :max="duration || 1"
          step="0.001"
          :value="time"
          :disabled="!duration"
          aria-label="同步视频进度"
          @pointerdown="beginScrub"
          @input="seek(Number(($event.target as HTMLInputElement).value))"
          @change="endScrub"
        /><span>{{ fmt(duration) }}</span>
        <label class="loop-control"><input type="checkbox" v-model="looping" />循环播放</label>
        <label class="speed-control"><select v-model.number="playbackRate" aria-label="同步播放速度"><option v-for="speed in speedOptions" :key="speed" :value="speed">{{ speed }}×</option></select></label>
        <button @click="fullscreen(comparison)">{{ fullscreenElement === comparison ? '关闭' : '全屏对比' }}</button>
      </div>
      </div>
      <div v-if="busy" class="phase-progress" aria-live="polite">
        <div v-for="(phase, index) in phases" :key="phase" :class="{ active: index === phaseIndex, complete: job?.status === 'done' || index < phaseIndex }">
          <span>{{ index + 1 }} · {{ phase }}</span>
          <small>{{ phaseDetail(index) }}</small>
          <progress max="100" :value="phasePercent(index)" :aria-label="`${phase}进度`"></progress>
        </div>
      </div>
      <div v-if="busy && job" class="progress-area">
        <div>
          <span>{{ job.stage }}</span
          ><span
            >{{ job.completed_frames ?? 0 }} /
            {{ job.total_frames ?? job.metadata?.frames ?? "—" }} 帧 ·
            {{ percent }}%</span
          >
        </div>
        <progress :value="percent" max="100" aria-label="处理进度"></progress
        ><small v-if="job.elapsed_seconds"
          >用时 {{ job.elapsed_seconds }} 秒</small
        >
        <small v-if="remaining" class="remaining">{{ remaining }}</small>
      </div>
      <footer class="export">
        <div>
          <strong>{{
            job?.metadata
              ? `${job.metadata.width} × ${job.metadata.height} · ${job.metadata.fps.toFixed(2)} fps`
              : "原分辨率处理 · 不自动缩小"
          }}</strong
          ><small>不覆盖原文件 · 自动保留原片与 PNG 遮罩母版 · 输出静音</small>
        </div>
        <button v-if="busy && job" @click="cancel">取消处理</button
        ><a
          v-if="exportDownload && !exportPending"
          class="primary"
          :href="exportDownload"
          >↓ 下载{{ exportLabel }}</a
        ><button v-else class="primary" disabled>{{ exportPending ? '正在准备导出…' : `↓ 下载${exportLabel}` }}</button>
      </footer>
    </section>
    <section v-if="history.length || historyMessage" class="history">
      <h2>最近的处理任务</h2>
      <button
        v-for="item in history"
        :key="item.id"
        :disabled="busy && job?.id !== item.id"
        @click="selectJob(item)"
      >
        <span>{{ item.name }}</span
        ><small>{{
          item.status === "done"
            ? "已完成"
            : item.status === "failed"
              ? `失败：${item.error || item.stage || '未知原因'}`
              : item.status === "cancelled"
                ? "已取消"
                : item.stage
        }}</small>
      </button>
      <button class="clear-history" :disabled="busy || clearingHistory || !history.length" @click="clearHistory">{{ clearingHistory ? '正在清空…' : '清空记录' }}</button>
      <p v-if="historyMessage" class="history-message" role="status">{{ historyMessage }}</p>
    </section>
    <section class="workflow"><h2>一个视频，四步完成背景移除</h2><div class="workflow-steps"><div v-for="[title, description] in [['上传素材', '选择或拖入原视频，文件仅保存在本机。'], ['每帧分离', '自动识别首帧人物，再逐帧生成细腻的透明遮罩。'], ['高清合成', '原始画面与 Alpha 合成，保留原分辨率，不自动超分。'], ['预览下载', '左右对比检查细节，按选定模式下载透明或合成视频。']]" :key="title"><h3>{{ title }}</h3><p>{{ description }}</p></div></div></section>
    <p class="footnote">
      精细抠像不等于画面超分。请检查发丝、手指、衣袖和裙摆；复杂遮挡、道具和多人素材可能需要进一步修正。CPU
      模式会明显慢于 NVIDIA GPU。
    </p>
    <footer class="copyright">©2026 izcw</footer>
  </main>
</template>
