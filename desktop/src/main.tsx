import React from 'react'
import ReactDOM from 'react-dom/client'
import { invoke } from '@tauri-apps/api/core'
import App from './App'
import { clearLegacySettings, readLegacySettings } from './format'
import type { InitialSettings, SettingsFile } from './types'
import './index.css'

// 启动引导:设置要在**渲染之前**读出来,否则会先闪一帧默认主题与默认语言。
// 读不到(纯浏览器里跑 npm run dev、或 IPC 桩没实现这条命令)就退回空设置,
// 界面照常可用 —— 设置读不到不该等于应用打不开。
async function bootstrap(): Promise<InitialSettings> {
  let file: SettingsFile | null = null
  let baseline: Record<string, boolean> = {}
  try {
    const [loaded, base] = await Promise.all([
      invoke<SettingsFile | null>('load_settings'),
      invoke<Record<string, boolean> | null>('clean_baseline'),
    ])
    file = loaded ?? null
    baseline = base ?? {}
  } catch { /* 非 Tauri 环境忽略 */ }

  if (!file) {
    // 文件不存在 = 从 localStorage 时代升上来的老用户,把旧设置搬过去。
    // 迁移只在这个条件下发生一次:写成功之后文件就存在了。
    const legacy = readLegacySettings()
    if (legacy) {
      file = legacy
      try {
        await invoke('save_settings', { settings: legacy })
        clearLegacySettings()
      } catch { /* 写不进去就先用着内存里这份,下次再说 */ }
    }
  }
  return { file: file ?? {}, baseline }
}

void bootstrap().then(initial => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App initial={initial} />
    </React.StrictMode>,
  )
})
