/// <reference types="vite/client" />

// Vue 单文件组件的类型声明：tsc 不认识 .vue，靠这个让 import App from './App.vue' 有类型
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>
  export default component
}
