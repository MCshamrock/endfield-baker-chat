import type { CapacitorConfig } from '@capacitor/cli'

// Baker AI 4 —— 与旧客户端（com.perlica.endfieldbaker）完全隔离：
//   · 包名不同 → 安卓可以并存安装，不会覆盖旧版
//   · appName 不同 → 桌面图标/应用列表能分清
//   · Android 工程放在仓库根的 Android/（不是默认的 android/），
//     所以必须显式写 android.path；Capacitor 8 支持这个字段。
const config: CapacitorConfig = {
  appId: 'com.peilika.bakerai',
  appName: 'Baker AI 4',
  webDir: 'dist',
  android: {
    path: 'Android',
  },
  // APK 通过 @capacitor/filesystem 把对话历史写入应用数据目录的 JSON 文件
}

export default config
