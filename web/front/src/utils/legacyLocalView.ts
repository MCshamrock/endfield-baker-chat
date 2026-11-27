// legacyLocalView —— 让"本机存档"能看到旧分片里的对话（只读兜底）
//
// 为什么需要：3.2.2 起老前端把卡片写在 `cardsV2` 索引 + `card:<slot>` 分片里，
// 而启动恢复 `loadProject()` 只读旧的整库 `cards` 键。于是老用户升到新前端后，
// 「本机存档」里只剩 36 张内置空卡，看起来像"数据没了"（2026-11-18 实测）。
//
// 这里只在"当前一张有消息的卡都没有"时，才去读老分片并塞回 store；
// 不写库、不删除任何东西。正常用户（没旧数据 / 已有对话）完全无感。

import type { Card } from '../types/chat'
import { readLegacyLocal } from './legacyLocalData'

interface CardStore {
  cards: Card[]
  replaceAllCards(cards: Card[]): void
}

export async function hydrateLegacyCards(store: CardStore): Promise<boolean> {
  // 注意：内置占位卡本来就有 conversations（只是没有消息），
  // 所以要数**消息条数**，不能数 conversations —— 否则永远判定"已有数据"。
  const messages = store.cards.reduce(
    (n, card) =>
      n +
      (card.conversations ?? []).reduce((m, conv) => m + (conv.messages?.length ?? 0), 0),
    0,
  )
  if (messages > 0) return false
  try {
    const local = await readLegacyLocal()
    const cards = (local?.cards ?? []) as unknown as Card[]
    const usable = cards.filter(
      (card) =>
        Array.isArray(card.conversations) &&
        card.conversations.some((c) => Array.isArray(c.messages) && c.messages.length > 0),
    )
    if (usable.length === 0) return false
    store.replaceAllCards(usable)
    return true
  } catch {
    // 读老库失败就当没有；绝不让启动流程因为兜底而挂掉
    return false
  }
}
