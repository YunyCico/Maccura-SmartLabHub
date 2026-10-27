/**
 * v-scroll-limit：列表区域最多显示 N 条，其余通过滚轮向下查看。
 *
 * 为什么不用纯 CSS 写死 max-height：
 * 列表项高度不统一（钉钉待办 60px、个人日程自适应、Salesforce 审批项更高），
 * 写死高度要么留白要么切一半。��里在渲染后测量前 N 条的真实高度，
 * 再把容器高度设成"第 N+1 条刚好露不出来"的位置，做到**精确显示 6 条**。
 *
 * 用法：<ul class="sf-list" v-scroll-limit="6"> ... </ul>
 *
 * 行为：
 * - 条数 ≤ N：容器不加高度限制，内容自然展开（不出现空滚动条）
 * - 条数 > N：容器固定高度 + 纵向滚动条，滚轮可查看剩余条目
 * - 高度在数据刷新、窗口缩放后重新测量
 */
import type { Directive } from 'vue'

const SCROLLABLE = 'is-scrollable'

/** 测量并应用高度限制。 */
function applyLimit(el: HTMLElement, limit: number) {
  // 先解除上一轮的限制，才能量到真实高度
  el.classList.remove(SCROLLABLE)
  el.style.maxHeight = ''

  const children = Array.from(el.children) as HTMLElement[]
  if (children.length <= limit) return

  // 前 limit 条的实际高度之和。
  // 容器本身没有 padding（.todo-list / .remind-list / .sf-list 都是 margin:0;padding:0），
  // 所以这个和就是精确的内容高度。
  let height = 0
  for (let i = 0; i < limit; i++) {
    height += children[i].getBoundingClientRect().height
  }
  if (height <= 0) return

  el.style.maxHeight = `${Math.ceil(height)}px`
  el.classList.add(SCROLLABLE)
}

const scrollLimit: Directive<HTMLElement, number> = {
  mounted(el, binding) {
    const limit = Number(binding.value) || 6
    const update = () => applyLimit(el, limit)

    // 数据刷新后需要重测：指令的 updated 钩子会先跑，
    // 但布局可能还没稳定，用 rAF 推一帧再量更准。
    let frame = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(update)
    }

    el.__scrollLimitUpdate = schedule

    const onResize = () => schedule()
    window.addEventListener('resize', onResize)
    el.__scrollLimitCleanup = () => {
      window.removeEventListener('resize', onResize)
      cancelAnimationFrame(frame)
    }

    schedule()
  },
  updated(el, binding) {
    const limit = Number(binding.value) || 6
    const update = el.__scrollLimitUpdate
    if (update) update()
    else applyLimit(el, limit)
  },
  unmounted(el) {
    el.__scrollLimitCleanup?.()
    el.__scrollLimitCleanup = undefined
    el.__scrollLimitUpdate = undefined
  },
}

declare global {
  interface HTMLElement {
    __scrollLimitUpdate?: () => void
    __scrollLimitCleanup?: () => void
  }
}

export default scrollLimit
export { scrollLimit }
