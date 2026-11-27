// =============================================================================
// 总结"提示词占用"估算（summaryPromptBudget，2026-11-14 追加）
// -----------------------------------------------------------------------------
// 「精细度调整」的下限/上限依赖"提示词占上下文多少"：
//   - 后端模式：提示词写死在后端，前端已知常数 BACKEND_PROMPT_TOKENS=4000；
//   - 自定义 API：提示词由前端两条 system 消息 + 用户自定义覆盖组成，
//     优先用实测 usage 校准值（effectiveBackendPromptTokens 的缓存），
//     同时把用户当前自定义提示词（世界观 / 角色覆盖）估算进去 —— 取两者较大值，
//     避免"改了提示词但缓存没刷新"导致边界算小。
//
// 纯函数：调用方把两条 system 消息的原文传进来，便于设置页与请求链路共用。
// =============================================================================

import type { ApiMode } from '../stores/settings'
import {
  BACKEND_PROMPT_TOKENS,
  effectiveBackendPromptTokens,
} from '../constants/summaryBudget'
import { estimateText } from './tokenEstimate'

/**
 * 消息框架开销（token）
 *
 * 两条 system 消息的 role / 分隔符 / 协议外壳。实测值通常已包含在
 * usage.prompt_tokens 里；本地估算时补一个保守的小常数。
 */
const PROMPT_FRAME_OVERHEAD_TOKENS = 32

// 【2026-11-18 P0】原来的「旧版直连」参数（提示词 3000 / 窗口 512000）已删除：
// 旧版模式整条下线，前端不再有那条压缩链。
/**
 * 计算总结判据里的"提示词占用"
 *
 * @param mode          当前 API 模式
 * @param systemText    第一条 system 消息（固定提示词 + 世界观）
 * @param characterText 第二条 system 消息（角色提示词 + 用户覆盖 + 回复规则）
 * @returns 提示词 token（后端模式恒为 4000；自定义模式取实测/估算较大值）
 */
export function summaryPromptTokens(
  mode: ApiMode,
  systemText: string,
  characterText: string,
): number {
  // 后端：提示词在服务端，前端只知道写死的常数（需求明确要求用 4000）
  if (mode === 'backend') return BACKEND_PROMPT_TOKENS

  // 自定义 API：实测校准值（含用户当时的提示词）与当前提示词估算取大
  const estimated = estimateText(systemText) + estimateText(characterText)
    + PROMPT_FRAME_OVERHEAD_TOKENS
  return Math.max(effectiveBackendPromptTokens(), estimated)
}
