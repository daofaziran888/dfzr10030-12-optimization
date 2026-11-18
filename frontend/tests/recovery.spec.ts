import { expect, test } from '@playwright/test'

const RECOVERY = 'http://127.0.0.1:5173/recovery'

test('通道恢复续传：八成中断、只补未完成分片、并发确认仅一份生效', async ({ page }) => {
  test.setTimeout(180_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => { if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`) })

  await page.goto(RECOVERY)
  await expect(page.locator('[data-testid="recovery-manifests"] tbody tr').first()).toBeVisible()

  // 预置大文件场景：失败分片在 80% 位置且保留错误位置
  const seedFileRow = page.locator('[data-testid="recovery-files"] tbody tr').first()
  await expect(seedFileRow).toContainText('第 8 片')
  await expect(seedFileRow).toContainText('偏移 7168')

  // 上传一个新的中断文件（唯一文件名，避免重跑时与历史数据撞名）
  const uniqueName = `E2E中断大文件-${Date.now()}.csv`
  await page.getByTestId('open-upload-file').click()
  await expect(page.getByTestId('upload-modal')).toBeVisible()
  await page.getByTestId('upload-filename').fill(uniqueName)
  await page.getByTestId('upload-channel').fill('INGE-0001')
  await page.getByTestId('upload-shards').fill('10')
  await page.getByTestId('upload-shard-size').fill('1024')
  await page.getByTestId('upload-fail-ratio').fill('0.8')
  await page.getByTestId('upload-failed-line').fill('800')
  await page.getByTestId('submit-upload').click()
  await expect(page.getByTestId('upload-modal')).toHaveCount(0)

  const newFileRow = page.locator('[data-testid="recovery-files"] tbody tr', { hasText: uniqueName })
  await expect(newFileRow).toBeVisible()
  await expect(newFileRow).toContainText('第 8 片')
  const fileId = (await newFileRow.getAttribute('data-testid'))?.replace('file-row-', '') as string

  // 生成续传清单
  await page.locator(`[data-testid="gen-manifest-${fileId}"]`).click()
  await expect(page.getByTestId('manifest-modal')).toBeVisible()
  await page.getByTestId('submit-manifest').click()
  await expect(page.getByTestId('manifest-modal')).toHaveCount(0)

  const manifestRow = page.locator('[data-testid="recovery-manifests"] tbody tr', { hasText: uniqueName }).first()
  await expect(manifestRow).toBeVisible()
  await expect(manifestRow).toHaveAttribute('data-status', '待核对')
  const manifestId = (await manifestRow.getAttribute('data-testid'))?.replace('manifest-row-', '') as string

  // 台账出现 3 条待核对（失败片 + 2 个未完成片）
  const ledgerRows = page.locator('[data-testid="ledger-table"] tbody tr')
  await expect.poll(async () => ledgerRows.count()).toBeGreaterThanOrEqual(3)

  // 并发确认两次：第一份生效
  await page.locator(`[data-testid="confirm-${manifestId}"]`).click()
  await expect(manifestRow).toHaveAttribute('data-status', '待续传')
  // 确认按钮已消失，再没有可二次确认入口；直接调接口模拟并发第二次，应被拒绝
  const second = await page.request.post(`/api/recovery/manifests/${manifestId}/confirm`, { data: {} })
  const secondBody = await second.json()
  expect(secondBody.ok).toBe(false)
  expect(secondBody.accepted).toBe(false)

  // 按失败偏移续传（续传后页面自动打开详情）
  await page.locator(`[data-testid="resume-${manifestId}"]`).click()
  await expect(manifestRow).toHaveAttribute('data-status', '已完成')
  await expect(manifestRow).toContainText('10240')
  await expect(page.getByTestId('manifest-detail')).toBeVisible()

  // 详情：每个分片都有解析结果且唯一，失败分片不再保留错误
  const detailShards = page.locator('[data-testid^="detail-shard-"]')
  await expect(detailShards).toHaveCount(10)
  const resultIds = new Set<string>()
  for (let i = 1; i <= 10; i++) {
    const row = page.locator(`[data-testid="detail-shard-${i}"]`)
    await expect(row).toHaveAttribute('data-status', '已完成')
    resultIds.add((await row.locator('td').nth(5).innerText()).trim())
  }
  expect(resultIds.size).toBe(10)
  await page.getByRole('button', { name: '关闭' }).click()
  await expect(page.getByTestId('manifest-detail')).toHaveCount(0)

  // 通过「查看」按钮再次打开详情
  await page.locator(`[data-testid="detail-${manifestId}"]`).click()
  await expect(page.getByTestId('manifest-detail')).toBeVisible()
  await expect(page.locator('[data-testid^="detail-shard-"]')).toHaveCount(10)
  await page.getByRole('button', { name: '关闭' }).click()

  // 导出下载
  const downloadPromise = page.waitForEvent('download')
  await page.locator(`[data-testid="export-${manifestId}"]`).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toContain('resume-manifest')
  expect(download.suggestedFilename()).toContain('.csv')

  // 导入校验：失败分片缺错误位置必须被拒绝并保留错误位置
  const badManifest = {
    channel_code: 'INGE-0001',
    original_file: { filename: 'bad.csv', version: 'v1' },
    fail_offset: 0,
    shards: [{ shard_no: 1, status: '失败', offset_start: 0, offset_end: 10 }],
  }
  const badRes = await page.request.post('/api/recovery/manifests/import', { data: { manifest: badManifest } })
  const badBody = await badRes.json()
  expect(badBody.ok).toBe(false)
  expect(JSON.stringify(badBody.errors)).toContain('错误位置')
  expect(badBody.rejected_manifest.status).toBe('校验失败')

  // 合法导入成功
  const goodManifest = {
    channel_code: 'INGE-0001',
    original_file: { filename: 'good.csv', version: 'v1' },
    fail_offset: 1024,
    shards: [
      { shard_no: 1, source_file_id: 'ext', status: '已完成', offset_start: 0, offset_end: 1024, error_position: null },
      { shard_no: 2, source_file_id: 'ext', status: '失败', offset_start: 1024, offset_end: 2048,
        error_position: { byte_offset: 1024, line: 12, reason: '中断' } },
    ],
  }
  const goodRes = await page.request.post('/api/recovery/manifests/import', { data: { manifest: goodManifest } })
  const goodBody = await goodRes.json()
  expect(goodBody.ok).toBe(true)
  expect(goodBody.manifest.status).toBe('待核对')

  expect(browserErrors).toEqual([])
})

test('两种恢复做法：原文件前置与修订版本前置生成不同清单', async ({ page, request }) => {
  test.setTimeout(120_000)
  // 登记中段失败文件
  const fileRes = await request.post('/api/recovery/files', {
    data: { filename: '冲突文件.csv', channel_code: 'INGE-0001', total_shards: 8, shard_size: 256, failed_shard: 5, failed_line: 500 },
  })
  const file = (await fileRes.json()).file
  // 供应商同时上传修订版本
  const revRes = await request.post(`/api/recovery/files/${file.file_id}/revisions`, { data: { failed_shard: 5 } })
  const revision = (await revRes.json()).file

  const token = `e2e-${Date.now()}`
  const origRes = await request.post('/api/recovery/manifests', {
    data: { file_id: file.file_id, strategy: 'original_first', client_token: `${token}-orig`, revision_file_id: revision.file_id },
  })
  const revListRes = await request.post('/api/recovery/manifests', {
    data: { file_id: file.file_id, strategy: 'revision_first', client_token: `${token}-rev`, revision_file_id: revision.file_id },
  })
  const orig = (await origRes.json()).manifest
  const rev = (await revListRes.json()).manifest
  expect(orig.manifest_id).not.toBe(rev.manifest_id)
  expect(orig.resume_file_id).toBe(file.file_id)
  expect(rev.resume_file_id).toBe(revision.file_id)
  expect(orig.fail_offset).toBe(1024)
  expect(rev.version_boundary_offset).toBe(1024)

  // 页面上两份清单都可见，且修订清单剩余分片指向修订文件（页面展示短 ID）
  await page.goto(RECOVERY)
  const origRow = page.locator('[data-testid="recovery-manifests"] tbody tr', { hasText: orig.manifest_id.slice(-8) })
  const revRow = page.locator('[data-testid="recovery-manifests"] tbody tr', { hasText: rev.manifest_id.slice(-8) })
  await expect(origRow).toContainText('原文件偏移前置')
  await expect(revRow).toContainText('修订版本前置')
  await revRow.getByRole('button', { name: '查看' }).click()
  await expect(page.getByTestId('manifest-detail')).toBeVisible()
  for (const no of [5, 6, 7, 8]) {
    await expect(page.locator(`[data-testid="detail-shard-${no}"]`)).toContainText(revision.file_id.slice(-8))
  }
})

test('CSV 续传清单导出后可通过上传入口直接再次提交', async ({ page, request }) => {
  test.setTimeout(120_000)
  const fileRes = await request.post('/api/recovery/files', {
    data: { filename: '回环UI文件.csv', channel_code: 'INGE-0001', total_shards: 4, shard_size: 512, failed_shard: 2, failed_line: 208 },
  })
  const file = (await fileRes.json()).file
  const manifestRes = await request.post('/api/recovery/manifests', {
    data: { file_id: file.file_id, strategy: 'original_first', client_token: `ui-loop-${Date.now()}` },
  })
  const manifest = (await manifestRes.json()).manifest

  // 浏览器内导出 CSV 到本地临时文件
  await page.goto(RECOVERY)
  const downloadPromise = page.waitForEvent('download')
  await page.locator(`[data-testid="export-${manifest.manifest_id}"]`).click()
  const download = await downloadPromise
  const csvPath = await download.path()
  expect(csvPath).toBeTruthy()

  // 通过「导入清单校验」入口直接再次提交导出的 CSV
  await page.getByTestId('import-file-input').setInputFiles(csvPath as string)
  const newRow = page.locator('[data-testid="recovery-manifests"] tbody tr', { hasText: '回环UI文件.csv' })
  await expect(newRow.first()).toContainText('待核对')
})

test('接入通道页可查看恢复偏移、原文件与修订版本并导出', async ({ page, request }) => {
  test.setTimeout(120_000)
  // 先把预置种子清单推进到完成，恢复偏移到达文件末尾
  const listRes = await request.get('/api/recovery/manifests')
  const seed = (await listRes.json()).items.find((m: any) => m.original_file.filename === '供应商主数据_2026Q4.csv' && m.status === '待核对')
  if (seed) {
    await request.post(`/api/recovery/manifests/${seed.manifest_id}/confirm`, { data: {} })
    await request.post(`/api/recovery/manifests/${seed.manifest_id}/resume`, { data: {} })
  }
  await page.goto('http://127.0.0.1:5173/ingestion')
  const panel = page.getByTestId('recovery-panel')
  await expect(panel).toBeVisible()
  const firstRow = panel.locator('tbody tr', { hasText: '供应商主数据_2026Q4.csv' }).first()
  await expect(firstRow).toContainText('已完成')
  await expect(firstRow).toContainText('10240')
  const downloadPromise = page.waitForEvent('download')
  await firstRow.getByRole('button', { name: '导出续传清单' }).first().click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toContain('.csv')
})
