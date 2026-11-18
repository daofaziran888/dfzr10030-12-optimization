<template>
  <section class="page" data-module="recovery">
    <header class="page-head">
      <div>
        <h2>通道恢复偏移及续传清单</h2>
        <p class="page-desc">
          中断后按失败偏移只补传未完成分片，已完成分片不重复解析；清单同时记录原文件、通道与偏移，
          支持上传、导入校验与导出下载，失败文件保留错误位置。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-upload-file" @click="openUpload">上传数据文件</button>
        <button class="btn" type="button" data-testid="open-import-manifest" @click="triggerImport">导入清单校验</button>
        <input ref="importInput" data-testid="import-file-input" type="file" accept=".json,application/json,.csv,text/csv" style="display:none" @change="onImportFile" />
        <button class="btn ghost" type="button" data-testid="reload-all" @click="reloadAll">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card"><span class="stat-label">数据文件</span><strong class="stat-value">{{ files.length }}</strong></article>
      <article class="stat-card"><span class="stat-label">续传清单</span><strong class="stat-value">{{ manifests.length }}</strong></article>
      <article class="stat-card"><span class="stat-label">待核对台账</span><strong class="stat-value">{{ pendingLedgerCount }}</strong></article>
      <article class="stat-card"><span class="stat-label">已完成恢复</span><strong class="stat-value">{{ completedCount }}</strong></article>
    </div>

    <h3>接入通道数据文件与失败分片记录</h3>
    <table class="data-table" data-testid="recovery-files">
      <thead>
        <tr>
          <th>文件名</th><th>版本</th><th>通道编号</th><th>分片数</th>
          <th>失败分片</th><th>失败偏移 / 错误位置</th><th>修订自</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="file in files" :key="file.file_id" :data-testid="`file-row-${file.file_id}`">
          <td>{{ file.filename }}</td>
          <td>{{ file.version }}</td>
          <td>{{ file.channel_code }}</td>
          <td>{{ file.total_shards }}</td>
          <td>
            <span v-if="file.failed_shard">第 {{ file.failed_shard }} 片</span>
            <span v-else>—</span>
          </td>
          <td>
            <template v-if="failedShard(file)">
              偏移 {{ failedShard(file)?.offset_start }} · 行 {{ failedShard(file)?.error_position?.line }}
            </template>
            <span v-else>—</span>
          </td>
          <td>{{ file.revision_of ? shortId(file.revision_of) : '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`gen-manifest-${file.file_id}`" @click="openManifest(file)">生成续传清单</button>
            <button class="link" type="button" :data-testid="`revise-${file.file_id}`" @click="uploadRevision(file)">上传修订版本</button>
          </td>
        </tr>
        <tr v-if="!files.length"><td colspan="8" class="empty-state">暂无数据文件，可先上传接入通道数据文件</td></tr>
      </tbody>
    </table>

    <h3 style="margin-top:20px">续传数据批次清单</h3>
    <table class="data-table" data-testid="recovery-manifests">
      <thead>
        <tr>
          <th>清单编号</th><th>通道</th><th>恢复做法</th><th>原文件 / 版本</th><th>修订版本</th>
          <th>失败偏移</th><th>恢复偏移</th><th>分片进度</th><th>状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in manifests" :key="m.manifest_id" :data-testid="`manifest-row-${m.manifest_id}`" :data-status="m.status">
          <td>{{ shortId(m.manifest_id) }}</td>
          <td>{{ m.channel_code }}</td>
          <td>{{ m.strategy_label }}</td>
          <td>{{ m.original_file.filename }} / {{ m.original_file.version }}</td>
          <td>{{ m.revision_file ? m.revision_file.version : '—' }}</td>
          <td>{{ m.fail_offset }}</td>
          <td>{{ m.recovery_offset }}</td>
          <td>{{ doneShardCount(m) }}/{{ m.total_shards }}</td>
          <td><span class="status-badge" :class="`badge-${m.status}`">{{ m.status }}</span></td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`detail-${m.manifest_id}`" @click="openDetail(m)">查看</button>
            <button v-if="m.status === '待核对'" class="link" type="button" :data-testid="`confirm-${m.manifest_id}`" @click="confirm(m)">确认清单</button>
            <button v-if="['待续传','续传中'].includes(m.status)" class="link" type="button" :data-testid="`resume-${m.manifest_id}`" @click="resume(m)">按失败偏移续传</button>
            <button v-if="m.status !== '校验失败'" class="link" type="button" :data-testid="`packages-${m.manifest_id}`" @click="packages(m)">字段包</button>
            <button v-if="m.status !== '校验失败'" class="link" type="button" :data-testid="`reconcile-${m.manifest_id}`" @click="reconcile(m)">核对同步</button>
            <button class="link" type="button" :data-testid="`export-${m.manifest_id}`" @click="exportManifest(m)">导出</button>
          </td>
        </tr>
        <tr v-if="!manifests.length"><td colspan="10" class="empty-state">暂无续传清单</td></tr>
      </tbody>
    </table>

    <h3 style="margin-top:20px">待核对台账（相邻业务面按清单状态联动）</h3>
    <table class="data-table" data-testid="ledger-table">
      <thead><tr><th>台账编号</th><th>清单</th><th>分片</th><th>来源文件</th><th>偏移区间</th><th>核对状态</th><th>清单状态</th><th>更新时间</th></tr></thead>
      <tbody>
        <tr v-for="entry in ledger" :key="entry.ledger_id" :data-testid="`ledger-entry-${entry.ledger_id}`">
          <td>{{ shortId(entry.ledger_id) }}</td>
          <td>{{ shortId(entry.manifest_id) }}</td>
          <td>第 {{ entry.shard_no }} 片</td>
          <td>{{ shortId(entry.source_file_id) }}</td>
          <td>{{ entry.offset_start }} ~ {{ entry.offset_end }}</td>
          <td>{{ entry.status }}</td>
          <td>{{ entry.manifest_status }}</td>
          <td>{{ entry.updated_at }}</td>
        </tr>
        <tr v-if="!ledger.length"><td colspan="8" class="empty-state">台账为空</td></tr>
      </tbody>
    </table>

    <!-- 上传数据文件 -->
    <div v-if="showUpload" class="modal-backdrop" data-testid="upload-modal">
      <form class="modal" @submit.prevent="submitUpload">
        <h3>上传接入通道数据文件</h3>
        <label class="create-field"><span>文件名</span><input v-model="uploadForm.filename" data-testid="upload-filename" required /></label>
        <label class="create-field"><span>通道编号</span><input v-model="uploadForm.channel_code" data-testid="upload-channel" required /></label>
        <label class="create-field"><span>分片数量</span><input v-model.number="uploadForm.total_shards" type="number" min="1" data-testid="upload-shards" required /></label>
        <label class="create-field"><span>分片大小（字节）</span><input v-model.number="uploadForm.shard_size" type="number" min="1" data-testid="upload-shard-size" required /></label>
        <label class="create-field"><span>失败分片（留空表示无中断）</span><input v-model.number="uploadForm.failed_shard" type="number" min="1" data-testid="upload-failed-shard" /></label>
        <label class="create-field"><span>或按中断比例（如 0.8 = 八成位置）</span><input v-model.number="uploadForm.fail_ratio" type="number" min="0" max="1" step="0.05" data-testid="upload-fail-ratio" /></label>
        <label class="create-field"><span>失败行号（保留错误位置）</span><input v-model.number="uploadForm.failed_line" type="number" min="1" data-testid="upload-failed-line" /></label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showUpload = false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-upload">上传并生成分片计划</button>
        </div>
      </form>
    </div>

    <!-- 生成续传清单（两种做法） -->
    <div v-if="showManifestModal" class="modal-backdrop" data-testid="manifest-modal">
      <form class="modal" @submit.prevent="submitManifest">
        <h3>生成通道恢复偏移及续传清单</h3>
        <p class="page-desc">目标文件：{{ manifestForm.filename }}（{{ manifestForm.version }}）</p>
        <label class="create-field"><span>恢复做法（取舍不同，清单不同）</span>
          <select v-model="manifestForm.strategy" data-testid="manifest-strategy">
            <option value="original_first">原文件偏移前置：按原文件失败偏移决定续传边界，旧结果保留可回查</option>
            <option value="revision_first">修订版本前置：切换修订文件并重新计算偏移，决定剩余数据内容</option>
          </select>
        </label>
        <label class="create-field"><span>修订文件（修订版本前置时必选）</span>
          <select v-model="manifestForm.revision_file_id" data-testid="manifest-revision">
            <option :value="null">无</option>
            <option v-for="rev in revisionOptions" :key="rev.file_id" :value="rev.file_id">
              {{ rev.version }}（{{ shortId(rev.file_id) }}）
            </option>
          </select>
        </label>
        <label class="create-field"><span>提交令牌（并发提交只确认一份）</span>
          <input v-model="manifestForm.client_token" data-testid="manifest-token" readonly />
        </label>
        <p v-if="manifestError" class="error-text" data-testid="manifest-error">{{ manifestError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showManifestModal = false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-manifest">生成清单并回写台账</button>
        </div>
      </form>
    </div>

    <!-- 清单详情 -->
    <div v-if="detail" class="modal-backdrop" data-testid="manifest-detail">
      <div class="modal" style="width:min(880px,100%)">
        <h3>清单详情 {{ shortId(detail.manifest_id) }}</h3>
        <p class="page-desc">{{ detail.boundary_note }}</p>
        <p v-if="detail.import_errors" class="error-text" data-testid="import-errors">
          导入校验失败 {{ detail.import_errors.length }} 处：
          <span v-for="(e, i) in detail.import_errors" :key="i">
            [{{ e.field }} 位置 {{ e.position }}：{{ e.reason }}]
          </span>
        </p>
        <table class="data-table">
          <thead><tr><th>分片</th><th>来源</th><th>偏移区间</th><th>状态</th><th>错误位置</th><th>解析结果</th></tr></thead>
          <tbody>
            <tr v-for="s in detail.shards" :key="s.shard_no" :data-testid="`detail-shard-${s.shard_no}`" :data-status="s.status">
              <td>第 {{ s.shard_no }} 片</td>
              <td>{{ shortId(s.source_file_id) }}</td>
              <td>{{ s.offset_start }} ~ {{ s.offset_end }}</td>
              <td>{{ s.status }}</td>
              <td>
                <template v-if="s.error_position">字节 {{ s.error_position.byte_offset }} / 行 {{ s.error_position.line }}</template>
                <span v-else>—</span>
              </td>
              <td>{{ s.parse_result_id ? shortId(s.parse_result_id) : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <h4 style="margin:12px 0 6px">字段包（格式解析服务仅读取未完成分片）</h4>
        <table v-if="detail.field_packages && detail.field_packages.length" class="data-table">
          <thead><tr><th>字段包</th><th>分片</th><th>偏移</th><th>字段</th></tr></thead>
          <tbody>
            <tr v-for="p in detail.field_packages" :key="p.package_id">
              <td>{{ shortId(p.package_id) }}</td><td>第 {{ p.shard_no }} 片</td>
              <td>{{ p.offset_start }} ~ {{ p.offset_end }}</td>
              <td>{{ Object.values(p.fields).join(' / ') }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="page-desc">尚未生成字段包</p>
        <h4 style="margin:12px 0 6px">状态历史</h4>
        <ul class="history-list" data-testid="detail-history">
          <li v-for="(h, i) in detail.history" :key="i">[{{ h.at }}] {{ h.from }} → {{ h.to }}：{{ h.note }}</li>
        </ul>
        <div class="page-actions"><button class="btn primary" type="button" @click="detail = null">关闭</button></div>
      </div>
    </div>

    <footer class="page-foot">
      <span>{{ message }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="recovery-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/recovery'
type Shard = Record<string, any>
type DataFile = Record<string, any>
type Manifest = Record<string, any>

const files = ref<DataFile[]>([])
const manifests = ref<Manifest[]>([])
const ledger = ref<any[]>([])
const message = ref('')
const errorMessage = ref('')

const showUpload = ref(false)
const uploadForm = ref<any>({})
const showManifestModal = ref(false)
const manifestForm = ref<any>({})
const manifestError = ref('')
const detail = ref<Manifest | null>(null)
const importInput = ref<HTMLInputElement | null>(null)

const pendingLedgerCount = computed(() => ledger.value.filter((e) => e.status === '待核对').length)
const completedCount = computed(() => manifests.value.filter((m) => m.status === '已完成').length)
const revisionOptions = computed(() =>
  files.value.filter((f) => f.revision_of === manifestForm.value.file_id))

function shortId(id: string) { return id ? String(id).slice(-8) : '—' }
function failedShard(file: DataFile): Shard | undefined {
  return (file.shards as Shard[]).find((s) => s.status === '失败')
}
function doneShardCount(m: Manifest) { return (m.shards as Shard[]).filter((s) => s.status === '已完成').length }

async function reloadAll() {
  try {
    const [fRes, mRes, lRes] = await Promise.all([
      request(`${ENDPOINT}/files`), request(`${ENDPOINT}/manifests`), request(`${ENDPOINT}/ledger`)])
    files.value = (await fRes.json()).items ?? []
    manifests.value = (await mRes.json()).items ?? []
    ledger.value = (await lRes.json()).items ?? []
  } catch (e: any) {
    errorMessage.value = e.message
  }
}

function openUpload() {
  uploadForm.value = { filename: '', channel_code: '', total_shards: 10, shard_size: 1024, failed_shard: null, fail_ratio: null, failed_line: null }
  showUpload.value = true
}

async function submitUpload() {
  errorMessage.value = ''
  const body: Record<string, any> = { ...uploadForm.value }
  if (!body.failed_shard) body.failed_shard = null
  if (body.fail_ratio === 0 || !body.fail_ratio) body.fail_ratio = null
  if (!body.failed_line) body.failed_line = null
  const res = await request(`${ENDPOINT}/files`, { method: 'POST', body: JSON.stringify(body) })
  const data = await res.json()
  if (!data.ok) { errorMessage.value = data.message; return }
  showUpload.value = false
  message.value = data.message
  await reloadAll()
}

async function uploadRevision(file: DataFile) {
  errorMessage.value = ''
  const res = await request(`${ENDPOINT}/files/${file.file_id}/revisions`, {
    method: 'POST', body: JSON.stringify({ failed_shard: file.failed_shard ?? null }),
  })
  const data = await res.json()
  if (!data.ok) { errorMessage.value = data.message; return }
  message.value = data.message
  await reloadAll()
}

function openManifest(file: DataFile) {
  manifestError.value = ''
  manifestForm.value = {
    file_id: file.file_id, filename: file.filename, version: file.version,
    strategy: 'original_first', revision_file_id: null,
    client_token: (crypto as any).randomUUID ? (crypto as any).randomUUID() : `token-${Date.now()}`,
  }
  showManifestModal.value = true
}

async function submitManifest() {
  manifestError.value = ''
  const res = await request(`${ENDPOINT}/manifests`, { method: 'POST', body: JSON.stringify(manifestForm.value) })
  const data = await res.json()
  if (!data.ok) { manifestError.value = data.message; return }
  showManifestModal.value = false
  message.value = data.message
  await reloadAll()
}

async function postAction(path: string, body: any = {}) {
  const res = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return res.json()
}

async function confirm(m: Manifest) {
  const data = await postAction(`${ENDPOINT}/manifests/${m.manifest_id}/confirm`, { client_token: m.confirm_token })
  message.value = data.message
  errorMessage.value = data.ok ? '' : data.message
  await reloadAll()
}

async function resume(m: Manifest) {
  const data = await postAction(`${ENDPOINT}/manifests/${m.manifest_id}/resume`)
  if (!data.ok) { errorMessage.value = data.message; return }
  message.value = data.message
  await reloadAll()
  await openDetailById(m.manifest_id)
}

async function packages(m: Manifest) {
  const data = await postAction(`${ENDPOINT}/manifests/${m.manifest_id}/field-packages`)
  if (!data.ok) { errorMessage.value = data.message; return }
  message.value = data.message
  await reloadAll()
  await openDetailById(m.manifest_id)
}

async function reconcile(m: Manifest) {
  const data = await postAction(`${ENDPOINT}/manifests/${m.manifest_id}/reconcile`)
  message.value = data.message
  if (!data.ok) errorMessage.value = data.message
  await reloadAll()
}

function exportManifest(m: Manifest) {
  window.open(`${ENDPOINT}/manifests/${m.manifest_id}/export`, '_blank')
  message.value = '续传清单已导出，下载后可直接再次提交导入'
}

async function openDetailById(manifestId: string) {
  const res = await request(`${ENDPOINT}/manifests/${manifestId}`)
  const data = await res.json()
  detail.value = data.manifest ?? null
}

async function openDetail(m: Manifest) { await openDetailById(m.manifest_id) }

function triggerImport() { importInput.value?.click() }

async function onImportFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  errorMessage.value = ''
  try {
    const text = await file.text()
    const isCsv = file.name.toLowerCase().endsWith('.csv')
    let data: any
    if (isCsv) {
      const res = await request(`${ENDPOINT}/manifests/import-csv`, { method: 'POST', body: JSON.stringify({ csv_text: text }) })
      data = await res.json()
    } else {
      const manifest = JSON.parse(text)
      const res = await request(`${ENDPOINT}/manifests/import`, { method: 'POST', body: JSON.stringify({ manifest }) })
      data = await res.json()
    }
    message.value = data.message
    if (!data.ok) {
      errorMessage.value = `${data.message}：${(data.errors || []).map((e: any) => `${e.field}@${e.position}`).join('，')}`
      detail.value = data.rejected_manifest
    }
    await reloadAll()
  } catch (e: any) {
    errorMessage.value = `导入文件解析失败：${e.message}`
  } finally {
    input.value = ''
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.status-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.badge-待核对 { background: #fef3c7; color: #92400e; }
.badge-待续传 { background: #dbeafe; color: #1e40af; }
.badge-续传中 { background: #e0e7ff; color: #3730a3; }
.badge-已完成 { background: #dcfce7; color: #166534; }
.badge-已拒绝, .badge-校验失败 { background: #fee2e2; color: #991b1b; }
.history-list { font-size: 12px; color: var(--muted); max-height: 140px; overflow: auto; padding-left: 18px; }
h3 { font-size: 15px; margin: 8px 0; }
h4 { font-size: 13px; }
</style>
