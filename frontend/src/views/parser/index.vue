<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>格式解析器管理</h2>
        <p class="page-desc">维护解析配置，围绕解析编号、输入格式、输出格式、字段映射做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记解析配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出格式解析器清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无格式解析器数据，可先登记解析配置</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记解析配置</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="recovery-panel" data-testid="parser-recovery-panel" style="margin-top:18px">
      <h3 style="font-size:15px;margin:0 0 8px">通道恢复偏移及续传清单（格式解析服务入口）</h3>
      <p class="page-desc">解析服务按续传清单只读取未完成分片生成字段包，核对清单并把可用状态同步给数据接入通道；清单状态变化会联动更新待核对台账。</p>
      <div class="page-actions" style="margin-bottom:8px">
        <RouterLink class="btn" to="/recovery">前往生成续传清单并回写台账</RouterLink>
        <button class="btn ghost" type="button" data-testid="parser-reload-recovery" @click="loadRecovery">刷新清单状态</button>
      </div>
      <table class="data-table">
        <thead><tr><th>清单编号</th><th>通道</th><th>恢复做法</th><th>失败/恢复偏移</th><th>字段包</th><th>清单状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="m in recoveryManifests" :key="m.manifest_id" :data-testid="`parser-manifest-${m.manifest_id}`">
            <td>{{ m.manifest_id }}</td>
            <td>{{ m.channel_code }}</td>
            <td>{{ m.strategy_label }}</td>
            <td>{{ m.fail_offset }} / {{ m.recovery_offset }}</td>
            <td>{{ (m.field_packages || []).length }}</td>
            <td>{{ m.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`parser-packages-${m.manifest_id}`" @click="parserAction(m, 'field-packages')">生成字段包</button>
              <button class="link" type="button" :data-testid="`parser-reconcile-${m.manifest_id}`" @click="parserAction(m, 'reconcile')">核对并同步通道</button>
            </td>
          </tr>
          <tr v-if="!recoveryManifests.length"><td colspan="7" class="empty-state">暂无续传清单</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条格式解析器记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/parser'
const columns = ["解析编号", "输入格式", "输出格式", "字段映射", "类型转换", "空值处理", "异常策略", "解析状态"]
const actions = ["提交配置", "启用解析", "废弃解析"]
const statuses = ["草稿", "已配置", "生效中", "已废弃"]
const metricNames = ["解析器总数", "生效中解析器", "已废弃解析器"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const recoveryManifests = ref<Record<string, any>[]>([])

async function loadRecovery() {
  try {
    const response = await request('/api/recovery/manifests')
    if (response.ok) recoveryManifests.value = (await response.json()).items ?? []
  } catch {
    recoveryManifests.value = []
  }
}

async function parserAction(manifest: Record<string, any>, action: string) {
  errorMessage.value = ''
  try {
    const response = await request(`/api/recovery/manifests/${manifest.manifest_id}/${action}`, { method: 'POST', body: '{}' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '操作失败')
    await loadRecovery()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析服务操作失败'
  }
}
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'parser.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('格式解析器动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式解析器列表读取失败'
  }
}

onMounted(() => {
  reload()
  loadRecovery()
})
</script>
