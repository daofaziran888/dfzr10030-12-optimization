<template>
  <section class="page" data-module="ingestion">
    <header class="page-head">
      <div><h2>数据接入通道</h2><p class="page-desc">维护接入通道，围绕通道编号、通道名称、输入格式、解析引擎做登记、筛选与状态流转。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记接入通道</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>
    <section class="recovery-panel" data-testid="recovery-panel">
      <h3 style="margin:16px 0 8px;font-size:15px;">通道恢复偏移与续传清单</h3>
      <p class="page-desc">中断后按失败偏移续接，已完成分片不重复；可查看恢复偏移、原文件与修订版本，导出清单后可直接再次提交。</p>
      <table class="data-table">
        <thead>
          <tr><th>通道</th><th>清单编号</th><th>原文件 / 版本</th><th>修订版本</th><th>失败偏移</th><th>恢复偏移</th><th>清单状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="m in recoveryManifests" :key="m.manifest_id" :data-testid="`ingestion-manifest-${m.manifest_id}`">
            <td>{{ m.channel_code }}</td>
            <td>{{ m.manifest_id }}</td>
            <td>{{ m.original_file.filename }} / {{ m.original_file.version }}</td>
            <td>{{ m.revision_file ? m.revision_file.version : '—' }}</td>
            <td>{{ m.fail_offset }}</td>
            <td>{{ m.recovery_offset }}</td>
            <td>{{ m.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`ingestion-export-${m.manifest_id}`" @click="exportManifest(m)">导出续传清单</button>
              <RouterLink class="link" to="/recovery">前往续传工作台</RouterLink>
            </td>
          </tr>
          <tr v-if="!recoveryManifests.length"><td colspan="8" class="empty-state">暂无恢复清单，可在续传工作台上传失败数据文件生成</td></tr>
        </tbody>
      </table>
    </section>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记接入通道</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/ingestion'
const columns = ["通道编号", "通道名称", "输入格式", "解析引擎", "并发数量", "背压阈值", "缓冲队列", "通道状态"]
const actions = ["开通通道", "暂停通道", "关闭通道"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const recoveryManifests = ref<any[]>([])
async function loadRecovery() {
  try {
    const res = await request('/api/recovery/manifests')
    if (res.ok) recoveryManifests.value = (await res.json()).items ?? []
  } catch (e) { recoveryManifests.value = [] }
}
function exportManifest(m: any) {
  window.open(`/api/recovery/manifests/${m.manifest_id}/export`, '_blank')
}
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(() => { reload(); loadRecovery() })
</script>
