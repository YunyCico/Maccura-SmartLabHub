/**
 * 头像处理。
 *
 * 为什么在前端裁切：头像到处都要显示（顶栏、账号列表），原始照片动辄几 MB，
 * 直接传上去会把库撑大、每次加载都要传一遍。这里统一压成 128×128 的 webp，
 * 一般 3~8KB —— 存成 data URL 放数据库里完全够用，也不用额外管静态文件。
 */

export const AVATAR_SIZE = 128;
/** 超过这个大小说明不是图片或文件太大，直接拒绝 */
const MAX_SOURCE_BYTES = 12 * 1024 * 1024;

export async function fileToAvatar(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) {
    throw new Error('请选择图片文件（PNG / JPG / WEBP）')
  }
  if (file.size > MAX_SOURCE_BYTES) {
    throw new Error('图片太大了，请选 12MB 以内的')
  }

  const bitmap = await loadImage(file)
  const canvas = document.createElement('canvas')
  canvas.width = AVATAR_SIZE
  canvas.height = AVATAR_SIZE
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('当前浏览器不支持图片处理')

  // 居中裁成正方形（cover），避免人脸被拉扁
  const side = Math.min(bitmap.width, bitmap.height)
  const sx = (bitmap.width - side) / 2
  const sy = (bitmap.height - side) / 2
  ctx.drawImage(bitmap, sx, sy, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE)

  // 先试 webp（体积小），不行退回 png
  let dataUrl = canvas.toDataURL('image/webp', 0.86)
  if (!dataUrl.startsWith('data:image/webp')) {
    dataUrl = canvas.toDataURL('image/png')
  }
  return dataUrl
}

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve(img)
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('图片读取失败，请换一张试试'))
    }
    img.src = url
  })
}
