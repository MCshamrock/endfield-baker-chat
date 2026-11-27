/**
 * main.ts —— 入口：挂上两套样式，然后把 App 挂到 #app。
 *
 * 样式顺序有讲究：
 *   1. endfield.css  套件（颜色、字体、切角、看板、表格，全部部件）
 *   2. panel.css     我们自己的布局（用套件的变量，不重写颜色）
 * 反过来会被套件的默认值盖掉一半。
 */
import { createApp } from 'vue'
import './assets/endfield/endfield.css'
import './styles/panel.css'
import App from './App.vue'

createApp(App).mount('#app')
