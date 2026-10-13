interface DingTalkBridge {
  ready: (callback: () => void) => void
  runtime: {
    permission: {
      requestAuthCode: (options: {
        corpId: string
        onSuccess: (result: { code?: string }) => void
        onFail: () => void
      }) => void
    }
  }
}

function bridge(): DingTalkBridge | undefined {
  return (window as unknown as { dd?: DingTalkBridge }).dd
}

export async function resolveDingTalkAuthCode(corpId: string): Promise<string | null> {
  const dd = bridge()
  if (!dd || !corpId) return null
  try {
    return await new Promise<string | null>((resolve) => {
      dd.ready(() => {
        dd.runtime.permission.requestAuthCode({
          corpId,
          onSuccess: (result) => resolve(result?.code || null),
          onFail: () => resolve(null),
        })
      })
    })
  } catch {
    return null
  }
}
