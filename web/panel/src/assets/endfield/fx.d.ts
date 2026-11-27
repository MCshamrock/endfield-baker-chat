/**
 * fx.d.ts —— 给套件里那个纯 JS 的小动效文件（fx.js）补类型。
 *
 * 为什么是"同名 .d.ts"而不是通配的 declare module：TypeScript 解析
 * `import { toast } from './endfield/fx.js'` 时会先找同目录的 fx.d.ts，
 * 把它当成那个模块的声明。写成顶层的 export 就对了；写成通配的
 * `declare module` 反而会变成"全局声明"、报 not a module。
 *
 * 为什么不用 allowJs：整个项目只有这一个是 JS，开 allowJs 会顺手把
 * 依赖里的一堆 JS 也拉进类型检查，得不偿失。
 */

export function scramble(el: HTMLElement, ms?: number): void
export function wireReadouts(root?: ParentNode): void
export function wireTheme(root?: Document, options?: { reveal?: boolean }): void
export function wireParallax(el: HTMLElement): void
export function wireDialogs(root?: ParentNode): void
export function toast(text: string, ms?: number): HTMLElement
export function removeRow(row: HTMLElement): Promise<void>
export function flashDone(btn: HTMLElement, label?: string, ms?: number): Promise<void>
export function closeDialog(dialog: HTMLDialogElement, value?: string): Promise<void>
