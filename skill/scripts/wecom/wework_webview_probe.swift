import Cocoa
import ApplicationServices

// 判断当前会话是不是「网页应用」：右侧显示网页而不是聊天消息表。
// 服务号 / 技术支持机器人 / 微盘 这类入口点开后都是网页视图，
// 没有可读的聊天记录，采不到任何东西。退出码：0=是网页应用，1=普通会话，3/4=环境异常。
func attr(_ e: AXUIElement, _ n: String) -> Any? {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success else { return nil }
    return v
}
func str(_ e: AXUIElement, _ n: String) -> String { (attr(e, n) as? String) ?? "" }
func kids(_ e: AXUIElement) -> [AXUIElement] { (attr(e, kAXChildrenAttribute as String) as? [AXUIElement]) ?? [] }
func rect(_ e: AXUIElement) -> CGRect {
    var p = CGPoint.zero, z = CGSize.zero
    if let v = attr(e, kAXPositionAttribute as String) { AXValueGetValue(v as! AXValue, .cgPoint, &p) }
    if let v = attr(e, kAXSizeAttribute as String) { AXValueGetValue(v as! AXValue, .cgSize, &z) }
    return CGRect(origin: p, size: z)
}
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.WeWorkMac").first else { exit(3) }
let ae = AXUIElementCreateApplication(app.processIdentifier)
guard let ws = attr(ae, kAXWindowsAttribute as String) as? [AXUIElement],
      let main = ws.first(where: { str($0, kAXTitleAttribute) == "企业微信" }) else { exit(4) }
let win = rect(main)
let paneStart = win.minX + win.width * 0.35          // 只看右侧内容区

// 只走容器节点。整棵树里有上万个行/文本节点，逐节点取位置和尺寸会拖到近 3 分钟；
// AXWebArea 挂在右侧内容区的容器链上（AXSplitGroup → AXScrollArea → AXWebArea），
// 跳过 AXRow/AXStaticText 等叶节点即可把遍历量压到几十个节点。
let skipRoles: Set<String> = ["AXRow", "AXStaticText", "AXImage", "AXButton", "AXCell",
                              "AXMenuItem", "AXCheckBox", "AXRadioButton", "AXTextField"]
var found = false
func walk(_ e: AXUIElement, _ d: Int) {
    if d > 10 || found { return }
    let r = str(e, kAXRoleAttribute)
    if r == "AXWebArea" {
        let f = rect(e)
        if f.minX >= paneStart && f.width > 200 && f.height > 300 {
            found = true
            print("网页应用：右侧网页 \(Int(f.width)) 宽")
            return
        }
    }
    if skipRoles.contains(r) { return }
    for c in kids(e) { walk(c, d + 1) }
}
walk(main, 0)
exit(found ? 0 : 1)
