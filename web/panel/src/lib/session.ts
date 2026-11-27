/**
 * session.ts —— 浏览器里那些"要记住的小事"：主题、登录后显示的名字。
 *
 * token 本身在 `api.ts` 的 browserTokenStore 里（它负责网络层的需要），
 * 这里只管界面层要记住的东西。**任何地方都不会把 token 打到 console**：
 * 排障时看 network 面板就够了，日志里留 token 等于把它抄在墙上。
 */

const THEME_KEY = 'baker.panel.theme'
const USER_KEY = 'baker.panel.user'

export type Theme = 'dark' | 'light'

/** 读主题：没存过就是 dark（后台默认暗）。 */
export function readTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

/** 写主题：同时改 <html data-theme>，样式表全靠这个属性切两套色。 */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  try {
    localStorage.setItem(THEME_KEY, theme)
  } catch {
    /* 存不下不影响本次会话 */
  }
}

/** 登录后记住"是谁"，只为了刷新时页头还能显示名字；权限仍然靠 token 现打现验。 */
export interface SavedUser {
  username: string
  displayName: string
}

export function saveUser(user: SavedUser): void {
  try {
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  } catch {
    /* 同上 */
  }
}

export function loadUser(): SavedUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<SavedUser>
    if (typeof parsed.username !== 'string') return null
    return { username: parsed.username, displayName: typeof parsed.displayName === 'string' ? parsed.displayName : '' }
  } catch {
    return null
  }
}

export function clearUser(): void {
  try {
    localStorage.removeItem(USER_KEY)
  } catch {
    /* 同上 */
  }
}
