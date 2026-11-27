// =============================================================================
// 智能总结(summary)
// -----------------------------------------------------------------------------
// 【2026-11-18 P0 后的定位】这是一段**通用**的"把一段对话交给 OpenAI 兼容端点
// 压成摘要"的工具函数。现在只有两处调用它：
//   1. 自定义 API 模式的手动 / 自动压缩（useContextCompaction）；
//   2. 自定义 API 模式的单聊自动总结走 utils/customSummary（同一思路，多了长度公式）。
//
// 默认模式（backend）**不用**它：新后端每轮自己压（memory.compress_to_fit），
// 前端再生成一次就是第二条压缩链（见 docs/08）。
//
// - 使用 OpenAI 兼容 /chat/completions 格式
// - 端点/Key/模型全部由调用方传入（getSummaryApi 决定），本文件不写死任何地址
// - 请求失败（配置错误/超时/返回异常）时抛错，由调用方决定怎么降级
// =============================================================================

/** 智能总结固定 system 提示词(前端内置,用户无需配置) */
export const SUMMARY_SYSTEM_PROMPT =
  '你是对话总结助手。请用简洁的中文总结以下对话内容，包括用户主要聊了什么、角色如何回应、关键事件或情绪变化。控制在300字以内。'

/** 总结请求超时(ms) */
const SUMMARY_TIMEOUT_MS = 20000

/** 总结输入长度上限(超出截断,避免请求体过大) */
const SUMMARY_INPUT_MAX = 6000

/**
 * 将历史消息按 user/assistant 格式拼成纯文本
 *
 * @param history 要总结的消息列表(side: mine→用户 / other→角色)
 * @returns 拼好的对话文本
 */
export function formatHistoryForSummary(
  history: Array<{ side: 'other' | 'mine'; text: string }>,
): string {
  const lines = history.map((h) => {
    const who = h.side === 'mine' ? '用户' : '角色'
    const content = h.text || '[图片]'
    return `${who}：${content}`
  })
  let text = lines.join('\n')
  if (text.length > SUMMARY_INPUT_MAX) {
    text = text.slice(-SUMMARY_INPUT_MAX)
  }
  return text
}

/**
 * 调用总结 API,返回总结文本
 *
 * @param baseUrl API Base URL(如 https://api.openai.com/v1)
 * @param apiKey  API Key
 * @param model   模型名
 * @param history 要总结的历史(前 N 条)
 * @returns       总结文本;失败抛出异常(由调用方降级)
 */
export async function requestSummary(
  baseUrl: string,
  apiKey: string,
  model: string,
  history: Array<{ side: 'other' | 'mine'; text: string }>,
): Promise<string> {
  // 配置缺失时直接抛错,由调用方降级 —— 避免拼出 "undefined/chat/completions"
  // 这种注定失败、且报错难懂的请求。
  // (custom 模式下 getSummaryApi 可能返回空配置:用户没填自定义 API,
  //  而自定义模式又禁止回退到后端 key 接口,见 stores/settings.ts。)
  if (!baseUrl || !apiKey || !model) {
    throw new Error('总结 API 未配置（自定义模式下请先在设置中填写 Base URL / API Key / 模型名）')
  }
  const url = `${baseUrl.replace(/\/+$/, '')}/chat/completions`
  const userContent = `以下是要总结的对话内容：\n\n${formatHistoryForSummary(history)}`

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), SUMMARY_TIMEOUT_MS)

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: SUMMARY_SYSTEM_PROMPT },
          { role: 'user', content: userContent },
        ],
        temperature: 0.3,
        max_tokens: 500,
      }),
      signal: controller.signal,
    })
    if (!response.ok) {
      throw new Error(`总结 API 请求失败 (${response.status})`)
    }
    const data = await response.json()
    const summary =
      data?.choices?.[0]?.message?.content ??
      data?.choices?.[0]?.text
    if (typeof summary !== 'string' || !summary.trim()) {
      throw new Error('总结 API 返回内容为空')
    }
    return summary.trim()
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('总结请求超时')
    }
    throw err
  } finally {
    window.clearTimeout(timer)
  }
}
