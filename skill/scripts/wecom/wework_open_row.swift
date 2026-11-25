//  按会话名打开左侧列表里的会话（点行，不用搜索框）
//
//  为什么不用搜索框：
//  1. 自动生成的长群名可能无法通过搜索框稳定定位；
//  2. 用 AXUIElementSetAttributeValue 写搜索框只会改文本，**不触发搜索**
//     （实测写值后列表既不筛选也不产生浮层）。
//
//  两种定位方式：
//  * 给 --index：先按序号跳过去（序号会随新消息漂移，漂移量由调用方用 --drift 累积补偿），
//    再在附近按名字确认；
//  * 不给 --index：先查当前视口；未命中则回顶部向下逐屏找。
//
//  用法：
//    swiftc -O wework_open_row.swift -o /tmp/openrow
//    /tmp/openrow --conversation "english(中文名)" --index 1440 --drift 0
//    /tmp/openrow --conversation "成员甲、成员乙、成员丙"      # 从当前位置向下找

import Cocoa
import ApplicationServices
import Foundation
// 输出重定向到日志文件时默认全缓冲（退出才落盘），实时控制台与卡住检测就看不到进展；改为逐行刷新。
_ = setvbuf(stdout, nil, _IOLBF, 0)

setvbuf(stdout, nil, _IONBF, 0)

func attr(_ e: AXUIElement, _ n: String) -> AnyObject? {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success else { return nil }
    return v as AnyObject?
}
func string(_ e: AXUIElement, _ n: String) -> String? {
    if let v = attr(e, n) as? String { return v }
    if let v = attr(e, n) as? NSAttributedString { return v.string }
    return nil
}
func role(_ e: AXUIElement) -> String { string(e, kAXRoleAttribute) ?? "" }
func children(_ e: AXUIElement) -> [AXUIElement] { (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }
func frameOf(_ e: AXUIElement) -> CGRect? {
    guard let p = attr(e, kAXPositionAttribute), let s = attr(e, kAXSizeAttribute) else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &pt), AXValueGetValue(s as! AXValue, .cgSize, &sz) else { return nil }
    return CGRect(origin: pt, size: sz)
}
func centerOf(_ e: AXUIElement) -> CGPoint? { frameOf(e).map { CGPoint(x: $0.midX, y: $0.midY) } }
func onAwakeDisplay(_ point: CGPoint) -> Bool {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return false }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return false }
    return ids.prefix(Int(count)).contains { CGDisplayIsAsleep($0) == 0 && CGDisplayBounds($0).contains(point) }
}
func requireOnScreen(_ point: CGPoint) {
    guard onAwakeDisplay(point) else {
        fputs("点击或滚动位置已移出所有唤醒的活动显示器；请把企业微信窗口移回屏幕\n", stderr)
        exit(22)
    }
}

var name = ""
var seekDate = ""
var seekDay = ""
var stopPrefix = ""
var indexHint = 0
var drift = 0
var maxScreens = 10
var debugGeo = false
var args = Array(CommandLine.arguments.dropFirst())
var i = 0
while i + 1 < args.count {
    switch args[i] {
    case "--conversation": name = args[i + 1]
    case "--seek-date": seekDate = args[i + 1]
    case "--seek-day": seekDay = args[i + 1]
    case "--stop-prefix": stopPrefix = args[i + 1]
    case "--index": indexHint = Int(args[i + 1]) ?? 0
    case "--drift": drift = Int(args[i + 1]) ?? 0
    case "--max-screens": maxScreens = Int(args[i + 1]) ?? 10
    case "--debug": debugGeo = (args[i + 1] == "1")
    default: break
    }
    i += 2
}
if name.isEmpty && seekDate.isEmpty { fputs("需要 --conversation 或 --seek-date\n", stderr); exit(64) }

guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): false] as CFDictionary) else {
    fputs("缺少辅助功能权限\n", stderr); exit(2)
}
// 锁屏/息屏时企业微信会把窗口收起来，AX 只回一个没有尺寸的 app 元素，
// 报错会变成「无法读取窗口尺寸」，极易被误判成「这个会话打不开」。
// 在这里先把环境问题挑明，别让环境故障伪装成会话问题。
let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
if (session["CGSSessionScreenIsLocked"] as? Int ?? 0) != 0 {
    fputs("屏幕已锁定（锁屏状态下合成事件会被系统吞掉，任何会话都打不开）。请先解锁屏幕再运行。\n", stderr)
    exit(20)
}
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.WeWorkMac").first else {
    fputs("企业微信未运行\n", stderr); exit(3)
}
let ae = AXUIElementCreateApplication(app.processIdentifier)
app.activate(options: [.activateAllWindows])
usleep(400000)
func ensureFrontmost() {
    for _ in 0..<5 {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier { return }
        app.activate(options: [.activateAllWindows])
        usleep(300_000)
    }
    fputs("企业微信未取得前台焦点，停止点击；检查是否有其他应用持续抢焦点\n", stderr)
    exit(23)
}
ensureFrontmost()
/// 诊断用：当前前台应用。点击/滚动失效时要能看出是不是别的应用（如执行采集的 AI 客户端）抢了前台。
func frontmostName() -> String {
    guard let f = NSWorkspace.shared.frontmostApplication else { return "未知" }
    return f.processIdentifier == app.processIdentifier ? "企业微信" : (f.localizedName ?? "pid \(f.processIdentifier)")
}

// BEGIN SHARED DIALOG HANDLING — kept identical in wework_open_row.swift and wework_history_capture.swift.
// Prompts (text + buttons, no list/search) are classified and handled; overlays with lists or search
// fields (history panel, global search) are not prompts and are left to the caller.
//   expiry   : an image/file expired notice with exactly one confirm button → confirm, keep capturing.
//   schedule : a 日程/会议 reminder → close first; if it stays, click the 稍后提醒 button.
//   unknown  : anything else → never clicked; described in the log so it can be reviewed.
enum DialogOutcome { case none, handled(Int), blocked(String) }
let dialogExpiryMarkers = ["已过期", "已被清理", "已失效"]
let dialogDeadSessionMarkers = ["已被你删除，无法查看内容", "已被删除，无法查看"]
let dialogRiskyWords = ["删除", "退出", "解散", "移除", "撤回", "清空", "发送", "转发", "是否", "确定要"]
let dialogConfirmWords: Set<String> = ["确定", "知道了", "我知道了", "好", "确认", "OK"]
let dialogCloseWords: Set<String> = ["关闭", "关闭提醒", "×", "✕", "X"]
let dialogDeferWords = ["稍后提醒", "分钟后提醒", "稍后"]
let dialogWindowButtons: Set<String> = ["AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"]
func dialogRole(_ n: AXUIElement) -> String { string(n, kAXRoleAttribute) ?? "" }
func dialogIsModal(_ n: AXUIElement) -> Bool {
    if dialogRole(n) == "AXSheet" { return true }
    let sub = string(n, kAXSubroleAttribute) ?? ""
    if sub == "AXDialog" || sub == "AXSystemDialog" { return true }
    return (attr(n, "AXModal") as? Bool) == true || (attr(n, "AXModal") as? NSNumber)?.boolValue == true
}
/// 输入法候选／状态浮窗同样是 AXDialog，但只有一个按钮、且按钮描述是输入法 bundle id；
/// 它不是业务弹窗，判成阻塞会让每个会话都失败。
func dialogIsInputMethodSurface(_ n: AXUIElement) -> Bool {
    for c in children(n) where dialogRole(c) == "AXButton" {
        let d = (string(c, kAXDescriptionAttribute) ?? "").lowercased()
        if d.contains("inputmethod") { return true }
    }
    return false
}
func dialogContainers(_ windows: [AXUIElement]) -> [AXUIElement] {
    var result: [AXUIElement] = []
    func walk(_ n: AXUIElement, _ depth: Int) {
        if depth > 5 { return }
        if dialogIsModal(n) {
            if dialogIsInputMethodSurface(n) { return }
            if !result.contains(where: { CFEqual($0, n) }) { result.append(n) }
            return
        }
        if dialogRole(n) == "AXTable" { return }
        for c in children(n) { walk(c, depth + 1) }
    }
    for w in windows { walk(w, 0) }
    return result
}
struct DialogParts { var texts: [String] = []; var buttons: [(AXUIElement, String)] = []; var closeButtons: [AXUIElement] = []; var overlay = false }
func dialogParts(_ dialog: AXUIElement) -> DialogParts {
    var parts = DialogParts()
    func walk(_ n: AXUIElement, _ d: Int) {
        if d > 10 { return }
        let r = dialogRole(n), sub = string(n, kAXSubroleAttribute) ?? ""
        if ["AXTable", "AXOutline", "AXTextArea", "AXList"].contains(r) || sub == "AXSearchField" { parts.overlay = true; return }
        if r == "AXStaticText" || r == "AXTextField" {
            let t = (string(n, kAXValueAttribute) ?? string(n, kAXTitleAttribute) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { parts.texts.append(t) }
        }
        if r == "AXButton" {
            let label = (string(n, kAXTitleAttribute) ?? string(n, kAXDescriptionAttribute) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if sub == "AXCloseButton" || dialogCloseWords.contains(label) { parts.closeButtons.append(n) }
            else if !dialogWindowButtons.contains(sub) { parts.buttons.append((n, label)) }
        }
        for c in children(n) { walk(c, d + 1) }
    }
    walk(dialog, 0)
    return parts
}
func dialogDescribe(_ p: DialogParts) -> String {
    let texts = p.texts.prefix(5).map { String($0.prefix(40)) }
    let buttons = p.buttons.map { $0.1.isEmpty ? "<无标题>" : $0.1 } + (p.closeButtons.isEmpty ? [] : ["<关闭>"])
    return "文字\(texts) 按钮\(buttons)"
}
func dialogHitBelongs(_ expected: AXUIElement, at point: CGPoint) -> Bool {
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit) == .success,
          var current = hit else { return false }
    for _ in 0..<12 {
        if CFEqual(current, expected) { return true }
        guard let raw = attr(current, kAXParentAttribute) else { return false }
        current = raw as! AXUIElement
    }
    return false
}
func dialogStillOpen(_ dialog: AXUIElement) -> Bool {
    let windows = (attr(ae, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    return dialogContainers(windows).contains { CFEqual($0, dialog) }
}
/// Press a button of this dialog: the AX action targets the exact element; the mouse fallback requires
/// the system hit test to land on that button. Returns true once the dialog is gone.
func dialogPress(_ button: AXUIElement, closing dialog: AXUIElement) -> Bool {
    if AXUIElementPerformAction(button, kAXPressAction as CFString) == .success {
        for _ in 0..<8 { usleep(100_000); if !dialogStillOpen(dialog) { return true } }
    }
    guard let f = frame(button), f.width > 0, f.height > 0 else { return false }
    let point = CGPoint(x: f.midX, y: f.midY)
    guard dialogHitBelongs(button, at: point) else { return false }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(60_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    for _ in 0..<10 { usleep(100_000); if !dialogStillOpen(dialog) { return true } }
    return false
}
func handleDialogs() -> DialogOutcome {
    var handled = 0
    for _ in 0..<6 {
        let windows = (attr(ae, kAXWindowsAttribute) as? [AXUIElement]) ?? []
        let prompts = dialogContainers(windows).map { ($0, dialogParts($0)) }.filter { !$0.1.overlay }
        guard let (dialog, parts) = prompts.first else { return handled == 0 ? .none : .handled(handled) }
        let joined = parts.texts.joined(separator: " ")
        let risky = dialogRiskyWords.contains { joined.contains($0) }
        // 「此面聊已被你删除，无法查看内容」是死路通知：删除早已发生，点「我知道了」没有副作用，
        // 不能因为文字含「删除」就当作危险弹窗，否则整轮被这类会话反复拦停。
        let deadSession = dialogDeadSessionMarkers.contains { joined.contains($0) }
        if deadSession, parts.buttons.count == 1, dialogConfirmWords.contains(parts.buttons[0].1) {
            print("处理弹窗：会话已删除提示，点击「\(parts.buttons[0].1)」")
            guard dialogPress(parts.buttons[0].0, closing: dialog) else { return .blocked("删除提示点击后未关闭：" + dialogDescribe(parts)) }
        } else if !risky, dialogExpiryMarkers.contains(where: { joined.contains($0) }), parts.buttons.count == 1,
           dialogConfirmWords.contains(parts.buttons[0].1) {
            print("处理弹窗：过期提示，点击「\(parts.buttons[0].1)」")
            guard dialogPress(parts.buttons[0].0, closing: dialog) else { return .blocked("过期提示点击后未关闭：" + dialogDescribe(parts)) }
        } else if !risky, joined.contains("日程") || joined.contains("会议") {
            let closed = parts.closeButtons.contains { dialogPress($0, closing: dialog) }
            if closed {
                print("处理弹窗：日程提醒，已关闭")
            } else if let later = parts.buttons.first(where: { b in dialogDeferWords.contains { b.1.contains($0) } }),
                      dialogPress(later.0, closing: dialog) {
                print("处理弹窗：日程提醒关闭不掉，已点击「\(later.1)」")
            } else {
                return .blocked("日程提醒关闭和稍后提醒都未生效：" + dialogDescribe(parts))
            }
        } else {
            return .blocked("未识别的弹窗：" + dialogDescribe(parts))
        }
        handled += 1
    }
    return .blocked("弹窗处理后仍持续出现")
}
// END SHARED DIALOG HANDLING
func frame(_ e: AXUIElement) -> CGRect? { frameOf(e) }
/// 回溯历史时读到过期图片会弹出提示，盖住会话列表吞掉点击。每次定位前先处理；处理不了只让本会话失败（25），不停整轮。
func resolveDialogsOrExit() {
    if case .blocked(let why) = handleDialogs() {
        fputs("弹窗阻塞本会话：\(why)\n", stderr); exit(25)
    }
}
resolveDialogsOrExit()


var focused: CFTypeRef?
if AXUIElementCopyAttributeValue(ae, kAXFocusedWindowAttribute as CFString, &focused) != .success || focused == nil {
    guard let first = (attr(ae, kAXWindowsAttribute) as? [AXUIElement])?.first else { fputs("没有可用窗口\n", stderr); exit(4) }
    focused = first as CFTypeRef
}
// 「查找聊天记录」面板/全局搜索浮层是独立的 AXWindow，采集结束后它会抢到焦点，
// 只从聚焦窗口找会话列表就会报「未定位到会话列表」（实测间歇性失败）。
// 因此逐个窗口找，全都找不到时先按 Esc 收掉浮层再试一轮。
// BEGIN SHARED SIDEBAR GEOMETRY — kept identical in the three standalone tools.
struct SidebarHit { let window: AXUIElement; let table: AXUIElement; let scroll: AXUIElement }
func scrollContainer(_ node: AXUIElement) -> AXUIElement? {
    var current = node
    for _ in 0..<8 {
        guard let raw = attr(current, kAXParentAttribute) else { return nil }
        current = raw as! AXUIElement
        if role(current) == "AXScrollArea" { return current }
        if role(current) == "AXWindow" { return nil }
    }
    return nil
}
func sidebarCandidates(in window: AXUIElement) -> [SidebarHit] {
    guard let wf = frameOf(window), wf.width > 600, wf.height > 300 else { return [] }
    var tables: [AXUIElement] = [], fields: [CGRect] = [], dividers: [CGRect] = []
    func walk(_ n: AXUIElement, _ depth: Int) {
        if depth > 12 { return }
        if role(n) == "AXTable" { tables.append(n); return }
        if role(n) == "AXTextField", let f = frameOf(n), f.width > 40,
           f.minY >= wf.minY, f.maxY < wf.minY + wf.height * 0.25 { fields.append(f) }
        if role(n) == "AXSplitter", let f = frameOf(n), f.height > f.width { dividers.append(f) }
        for c in children(n) { walk(c, depth + 1) }
    }
    walk(window, 0)
    let regions = tables.compactMap { t -> (AXUIElement, AXUIElement, CGRect)? in
        guard let s = scrollContainer(t), let f = frameOf(s), f.width > 0, f.height > 0 else { return nil }
        return (t, s, f)
    }
    return regions.compactMap { t, s, vp in
        // Require navigation on the left, a search field above, and a vertical divider on the right.
        // Loaded message counts and window width percentages do not identify a conversation list.
        let search = fields.contains { vp.minX - 6 <= $0.minX && $0.maxX <= vp.maxX + 6 &&
            $0.maxY <= vp.minY + 6 && vp.minY - $0.maxY < 100 }
        let navigation = regions.contains { other, _, f in !CFEqual(other, t) &&
            abs(f.maxX - vp.minX) <= 8 && f.minX < vp.minX && f.width < vp.width &&
            min(f.maxY, vp.maxY) - max(f.minY, vp.minY) > min(f.height, vp.height) * 0.5 &&
            !fields.contains(where: { f.minX - 6 <= $0.minX && $0.maxX <= f.maxX + 6 &&
                $0.maxY <= f.minY + 6 && f.minY - $0.maxY < 100 }) }
        let divider = dividers.contains { abs($0.midX - vp.maxX) <= 8 &&
            min($0.maxY, vp.maxY) - max($0.minY, vp.minY) > vp.height * 0.5 }
        guard search && navigation && divider else { return nil }
        return SidebarHit(window: window, table: t, scroll: s)
    }
}
func uniqueSidebar(_ windows: [AXUIElement]) -> SidebarHit? {
    var hits: [SidebarHit] = []
    for w in windows {
        for h in sidebarCandidates(in: w) where !hits.contains(where: { CFEqual($0.table, h.table) }) {
            hits.append(h)
        }
    }
    return hits.count == 1 ? hits.first : nil
}
func displayLayout() -> [String] {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return [] }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return [] }
    return ids.prefix(Int(count)).map { id in
        let f = CGDisplayBounds(id)
        return "\(id):\(f.minX):\(f.minY):\(f.width):\(f.height):\(CGDisplayIsAsleep(id))"
    }.sorted()
}
func layoutMatches(_ initialWindow: CGRect, _ initialViewport: CGRect, _ initialDisplays: [String],
                   _ currentWindow: CGRect?, _ currentViewport: CGRect?, _ currentDisplays: [String]) -> Bool {
    currentWindow == initialWindow && currentViewport == initialViewport && currentDisplays == initialDisplays
}
// END SHARED SIDEBAR GEOMETRY

func pressEscape() {
    if let down = CGEvent(keyboardEventSource: nil, virtualKey: 0x35, keyDown: true),
       let up = CGEvent(keyboardEventSource: nil, virtualKey: 0x35, keyDown: false) {
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

func windowCandidates() -> [AXUIElement] {
    var list: [AXUIElement] = []
    if let f = focused { list.append(f as! AXUIElement) }
    if let w = attr(ae, kAXWindowsAttribute) as? [AXUIElement] { list.append(contentsOf: w) }
    return list
}

var sidebar = uniqueSidebar(windowCandidates())
if sidebar == nil {
    pressEscape()
    usleep(400000)
    var retry = windowCandidates()
    if let fw = attr(ae, kAXFocusedWindowAttribute) { retry.insert(fw as! AXUIElement, at: 0) }
    sidebar = uniqueSidebar(retry)
}
guard let pick = sidebar else { fputs("未定位到会话列表\n", stderr); exit(6) }
let root = pick.window
let table = pick.table
guard let rf = frameOf(root), let viewport = frameOf(pick.scroll) else { fputs("无法读取窗口尺寸\n", stderr); exit(5) }
let sidebarRight = viewport.maxX
let initialDisplays = displayLayout()
func requireStableLayout() {
    guard layoutMatches(rf, viewport, initialDisplays, frameOf(root), frameOf(pick.scroll), displayLayout()) else {
        fputs("窗口或显示器布局已变化，停止定位；重新运行以建立新的窗口坐标\n", stderr)
        exit(22)
    }
}

// 搜索框里残留关键词会让列表被过滤，序号与内容错位（实测差 60 多行），必须先清空
func clearSearch() {
    requireStableLayout()
    var fields: [AXUIElement] = []
    func findField(_ n: AXUIElement, _ d: Int) {
        if d > 12 { return }
        if let f = frameOf(n), f.minX >= sidebarRight { return }
        if role(n) == "AXTable" { return }
        if role(n) == "AXTextField", let f = frameOf(n), f.minY < rf.minY + rf.height * 0.15 { fields.append(n) }
        for c in children(n) { findField(c, d + 1) }
    }
    findField(root, 0)
    for f in fields where !(string(f, kAXValueAttribute) ?? "").isEmpty {
        AXUIElementSetAttributeValue(f, kAXValueAttribute as CFString, "" as AnyObject)
        usleep(300000)
        print("已清空搜索框残留：\(string(f, kAXValueAttribute) ?? "")")
    }
}

func visibleRows() -> [AXUIElement] {
    if let rows = attr(table, "AXVisibleRows") as? [AXUIElement], !rows.isEmpty { return rows }
    return children(table).filter { role($0) == "AXRow" }
}
func rowIndex(_ r: AXUIElement) -> Int? { (attr(r, "AXIndex") as? NSNumber)?.intValue }
// 视口首行可能是缺 AXIndex 的半截行，直接取会让 delta 变成上千、朝错误方向狂滚
func firstIndex() -> Int { visibleRows().compactMap(rowIndex).first ?? -1 }
func rowNameOf(_ r: AXUIElement) -> String {
    // 单聊行里有两段文本：姓名（上）和岗位/部门（下），AX 子节点顺序不固定，
    // 直接取第一个会把岗位名当成会话名。按纵向位置取最上面那段才是姓名。
    // 实测：会话名文本高度 20pt，预览行与时间/岗位 18pt。字号是唯一稳定的区分信号——
    // 单聊行的岗位、群行的预览和时间戳都更小。取高度最大者，同高再取最上面那个。
    var out: [(CGFloat, CGFloat, String)] = []
    func walk(_ n: AXUIElement, _ d: Int) {
        if d > 6 { return }
        if role(n) == "AXStaticText", let v = string(n, kAXValueAttribute), !v.isEmpty, let f = frameOf(n) {
            out.append((f.height, f.minY, v))
        }
        for c in children(n) { walk(c, d + 1) }
    }
    walk(r, 0)
    guard let best = out.max(by: { a, b in
        abs(a.0 - b.0) < 0.5 ? a.1 > b.1 : a.0 < b.0      // 高度优先；等高时 y 更小者（更靠上）胜
    }) else { return "" }
    return best.2
}

/// 归一化：界面会把长群名截断并补「…」，候选表里的名字与界面值常常只差一个省略号，
/// 直接全等比较会把「本来能找到」的行判成找不到。
func norm(_ s: String) -> String {
    var t = s
    for junk in ["…", "...", "⋯"] { t = t.replacingOccurrences(of: junk, with: "") }
    return t.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// 身份判定（唯一入口）：全等 → 截断前缀 → 截断后缀 → 反向截断。
/// 所有「这一行是不是我要的会话」的判断都必须走这里，别各写各的。
func nameMatch(_ rowName: String, _ target: String) -> Bool {
    if rowName.isEmpty || target.isEmpty { return false }
    if rowName == target { return true }
    let a = norm(rowName), b = norm(target)
    if a.isEmpty || b.isEmpty { return false }
    if a == b { return true }
    if b.hasPrefix(a) || b.hasSuffix(a) || a.hasPrefix(b) { return true }
    // 自动群名随成员进出变化（有人走、也有人来），名字既不是前缀也不是后缀。
    // 按共同成员占比判定，与 fuzzyRow 同一标准；调用处要求候选唯一才落点。
    let want = memberSet(target), got = memberSet(rowName)
    if want.count >= 3, got.count >= 2 {
        let common = want.intersection(got).count
        if common >= 2, Double(common) / Double(max(want.count, got.count)) >= 0.6 { return true }
    }
    return false
}

func rowTime(_ r: AXUIElement) -> String {
    var out: [(CGFloat, String)] = []
    func walk(_ n: AXUIElement, _ d: Int) {
        if d > 6 { return }
        if role(n) == "AXStaticText", let v = string(n, kAXValueAttribute), !v.isEmpty, let f = frameOf(n) {
            out.append((f.minX, v))
        }
        for c in children(n) { walk(c, d + 1) }
    }
    walk(r, 0)
    let midX = (frameOf(table) ?? rf).midX
    return out.filter { $0.0 > midX }.map { $0.1 }.first
        { $0.range(of: #"^(\d{4}/)?\d{1,2}/\d{1,2}$"#, options: .regularExpression) != nil
            || $0.range(of: #"^\d{1,2}月\d{1,2}日$"#, options: .regularExpression) != nil
            || $0.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil
            || $0.range(of: #"^(星期|周)[一二三四五六日天]$"#, options: .regularExpression) != nil
            || $0.range(of: #"^(昨天|前天)$"#, options: .regularExpression) != nil
            || $0.range(of: #"^\d+\s*(分钟|小时)前$"#, options: .regularExpression) != nil } ?? ""
}

// 只在布局稳定期间输入；移动、缩放或显示器变化都停止，续跑重新建立坐标。
func currentAnchor() -> CGPoint? {
    requireStableLayout()
    guard let tf = frameOf(table), let wfr = frameOf(root) else { return nil }
    let p = CGPoint(x: tf.midX, y: wfr.midY)
    return onAwakeDisplay(p) ? p : nil
}
guard let initialAnchor = currentAnchor() else {
    fputs("会话列表已不在可见显示器上；请把企业微信窗口移回屏幕\n", stderr)
    exit(22)
}
requireOnScreen(initialAnchor)
func wheel(_ lines: Int32, settleMs: UInt32 = 90) {
    ensureFrontmost()
    guard let anchor = currentAnchor() else {
        fputs("滚动过程中窗口离开了可见显示器（例如拔掉扩展屏），停止本会话；重新运行即可继续\n", stderr)
        exit(22)
    }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: anchor, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(15000)
    let ev = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: lines, wheel2: 0, wheel3: 0)
    ev?.location = anchor
    ev?.post(tap: .cghidEventTap)
    usleep(settleMs * 1000)
}
/// 每次都重新读 AX 树定位，绝不复用上一次的坐标或 AXUIElement：
/// 列表会随新消息重排，序号会漂移，缓存下来的元素随时可能指向另一个会话。
func findRow() -> (AXUIElement, Int)? {
    requireStableLayout()
    let rows = visibleRows()
    if name.isEmpty, indexHint > 0 {
        let hits = rows.filter { rowIndex($0) == indexHint + drift }
        if hits.count > 1 {
            fputs("会话序号歧义：AXIndex \(indexHint + drift) 命中 \(hits.count) 行，停止以免点错\n", stderr)
            exit(12)
        }
        return hits.first.map { ($0, rowIndex($0) ?? 0) }
    }
    let exact = rows.filter { rowNameOf($0) == name }
    if exact.count > 1 {
        // 同名会话宁可停下让人确认，也不能靠「取第一个」蒙——采错了整段数据都是别人的
        fputs("会话标题歧义：视口内 \(exact.count) 行名为「\(name)」，停止以免点错\n", stderr)
        exit(12)
    }
    if let r = exact.first { return (r, rowIndex(r) ?? 0) }
    // 很长的自动群名在界面上会被截断（末尾补「…」），退化为「归一化后互为前缀/后缀」。
    // 多行命中时取「行名最长」的一条——截断越少信息越多，最可能是真身；
    // 最长也并列就停下，宁可让人确认也不能蒙。
    let fuzzy = rows.filter { nameMatch(rowNameOf($0), name) }
    if fuzzy.isEmpty { return nil }
    let sorted = fuzzy.sorted { norm(rowNameOf($0)).count > norm(rowNameOf($1)).count }
    if sorted.count > 1,
       norm(rowNameOf(sorted[0])).count == norm(rowNameOf(sorted[1])).count,
       rowIndex(sorted[0]) != rowIndex(sorted[1]) {
        fputs("会话标题截断后仍歧义：「\(name)」匹配到 \(sorted.count) 行，停止以免点错\n", stderr)
        exit(12)
    }
    return (sorted[0], rowIndex(sorted[0]) ?? 0)
}

clearSearch()
// 搜索框仍持有焦点时，点列表行不会切换会话（实测：先点一下右侧消息区才生效）。
// 以前每次打开都盲点消息区中部，会点开上一会话里的链接/卡片/图片（2026-10-31 01:46 累积打开
// 5 个文章窗口、侧栏被切走）。现在只在侧栏搜索框确实持有焦点时才转移焦点：先用 AX 把焦点交给
// 会话列表；仍不行才点击，且落点必须经命中测试确认是不可交互的空白/文本区域。
func sidebarSearchFocused() -> Bool {
    guard let focused = attr(ae, kAXFocusedUIElementAttribute) else { return false }
    var current = focused as! AXUIElement
    for _ in 0..<4 {
        if role(current) == "AXTextField", let f = frameOf(current), f.minX < sidebarRight, f.minY < rf.minY + rf.height * 0.15 {
            return true
        }
        guard let parent = attr(current, kAXParentAttribute) else { return false }
        current = parent as! AXUIElement
    }
    return false
}
func pointIsInert(_ p: CGPoint) -> Bool {
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(p.x), Float(p.y), &hit) == .success,
          var current = hit else { return false }
    var pid: pid_t = 0
    guard AXUIElementGetPid(current, &pid) == .success, pid == app.processIdentifier else { return false }
    let interactive: Set<String> = ["AXLink", "AXButton", "AXImage", "AXWebArea", "AXMenuButton", "AXPopUpButton",
                                    "AXCheckBox", "AXRadioButton", "AXTextField", "AXSheet", "AXDialog"]
    for _ in 0..<10 {
        let r = role(current)
        if interactive.contains(r) { return false }
        if r == "AXWindow" { return true }
        var actions: CFArray?
        if AXUIElementCopyActionNames(current, &actions) == .success,
           ((actions as? [String]) ?? []).contains(kAXPressAction as String) { return false }
        guard let parent = attr(current, kAXParentAttribute) else { return true }
        current = parent as! AXUIElement
    }
    return true
}
if sidebarSearchFocused() {
    AXUIElementSetAttributeValue(table, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(200000)
}
if sidebarSearchFocused() {
    let spots = [0.5, 0.35, 0.65, 0.25, 0.75].map { CGPoint(x: rf.minX + rf.width * 0.62, y: rf.minY + rf.height * $0) }
    if let away = spots.first(where: { pointIsInert($0) }) {
        requireOnScreen(away)
        CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: away, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(100000)
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: away, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(80000)
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: away, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(600000)
    } else {
        print("侧栏搜索框持有焦点，但消息区没有可安全点击的空白处；不盲点，继续按行打开")
    }
}

// 3) 按日期定位：列表严格按最后活跃时间倒序，所以「日期」比「序号」稳。
//    先按序号跳个大概，再按日期微调方向，直到视口里出现目标日期的行。
func dateKey(_ t: String) -> Int {
    // 今年内是 M/D（如 1/31），跨年是 YYYY/M/D；越大越新
    let p = t.split(separator: "/").compactMap { Int($0) }
    if t.contains("/") == false { return 0 }
    if p.count == 3 { return p[0] * 10000 + p[1] * 100 + p[2] }
    if p.count == 2 { return 3000 * 10000 + p[0] * 100 + p[1] }   // 今年统一排在跨年之后
    return 0
}
func findRowWithDate(_ d: String) -> (AXUIElement, Int)? {
    for r in visibleRows() where rowTime(r) == d { return (r, rowIndex(r) ?? 0) }
    return nil
}

// 侧栏的时间文本有四种形态：HH:MM（今天）、昨天、星期X（近一周）、M/D（本年）、YYYY/M/D（跨年）。
// 扫描时刻不同，同一会话的文本会变，所以按「绝对日期」比较而不是按字符串比较：
// 用当前系统时间把每行文本还原成 YYYYMMDD，再与目标日期比对。跨午夜也成立，
// 因为还原始终用「此刻」的日历，而目标日期是扫描时写死的绝对日期。
let dayCalendar = Calendar.current
let dayNow = Date()
func dayInt(_ d: Date) -> Int {
    let c = dayCalendar.dateComponents([.year, .month, .day], from: d)
    return (c.year ?? 0) * 10000 + (c.month ?? 0) * 100 + (c.day ?? 0)
}
func dayOfISO(_ s: String) -> Int? {
    let p = s.split(separator: "-").compactMap { Int($0) }
    guard p.count == 3 else { return nil }
    return p[0] * 10000 + p[1] * 100 + p[2]
}
func dayOfText(_ raw: String) -> Int? {
    let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty { return nil }
    if s.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil { return dayInt(dayNow) }
    // 「12分钟前」「3小时前」必然是今天；跨午夜时最多差到昨天，按今天处理仍在一天误差内
    if s.range(of: #"^\d+\s*(分钟|小时)前$"#, options: .regularExpression) != nil { return dayInt(dayNow) }
    if s == "昨天" { return dayInt(dayCalendar.date(byAdding: .day, value: -1, to: dayNow) ?? dayNow) }
    if s == "前天" { return dayInt(dayCalendar.date(byAdding: .day, value: -2, to: dayNow) ?? dayNow) }
    let weekMap: [String: Int] = ["日": 1, "一": 2, "二": 3, "三": 4, "四": 5, "五": 6, "六": 7]
    if s.hasPrefix("星期") || s.hasPrefix("周") {
        if let want = weekMap[String(s.suffix(1))] {
            // Calendar: 1=周日 … 7=周六。侧栏只在 2–7 天前用星期，故同日视为 7 天前。
            let current = dayCalendar.component(.weekday, from: dayNow)
            var back = (current - want + 7) % 7
            if back == 0 { back = 7 }
            return dayInt(dayCalendar.date(byAdding: .day, value: -back, to: dayNow) ?? dayNow)
        }
    }
    let parts = s.split(separator: "/").compactMap { Int($0) }
    if parts.count == 3 { return parts[0] * 10000 + parts[1] * 100 + parts[2] }
    if parts.count == 2 {
        let year = dayCalendar.component(.year, from: dayNow)
        return year * 10000 + parts[0] * 100 + parts[1]
    }
    return nil
}
/// 取「日期单调不增」的最长后缀：顶部置顶块的日期顺序是乱序的，不能参与插值。
func monotonicTail(_ rows: [(Int, Int)]) -> [(Int, Int)] {
    guard rows.count > 1 else { return rows }
    var start = rows.count - 1
    while start > 0 && rows[start - 1].1 >= rows[start].1 { start -= 1 }
    return Array(rows[start...])
}
/// 视口内所有「能解析出日期」的行，返回 (行号, 日期)，行号小=更靠上=更新。
func datedRows() -> [(Int, Int)] {
    visibleRows().compactMap { r in
        guard let idx = rowIndex(r), let day = dayOfText(rowTime(r)) else { return nil }
        return (idx, day)
    }
}
var presetRow: (AXUIElement, Int)?
// 目标已经可见时直接按名命中，避免粗定位将它滚出视口。
presetRow = findRow()
if presetRow == nil && indexHint <= 0 && seekDate.isEmpty {
    if let parent = attr(table, kAXParentAttribute) {
        let scroll = parent as! AXUIElement
        if let bar = children(scroll).first(where: { role($0) == "AXScrollBar" && string($0, kAXOrientationAttribute) != "AXHorizontalOrientation" }) {
            _ = AXUIElementSetAttributeValue(bar, kAXValueAttribute as CFString, NSNumber(value: 0))
            usleep(350_000)
        }
    }
    presetRow = findRow()
}
if seekDay.isEmpty && indexHint > 0 && presetRow == nil {
    let wanted = indexHint + drift
    let startIndex = firstIndex()
    // 客户端滚动有节流，单轮常常不到位；分 3 轮收敛，每轮结束都等足 1.2s 再读
    outer: for pass in 1...3 {
        for _ in 1...40 {
            let cur = firstIndex()
            if cur <= 0 { wheel(-30, settleMs: 300); continue }
            let delta = wanted - cur
            if delta >= 0 && delta <= 5 { break }
            let lines = max(12, min(72, Int(Double(abs(delta)) * 2.7)))
            wheel(delta < 0 ? Int32(lines) : -Int32(lines))
        }
        usleep(1200000)
        let cur = firstIndex()
        if wanted >= cur && wanted - cur <= 5 {
            print("定位：首行 AXIndex=\(cur)，目标 \(wanted)（第 \(pass) 轮收敛）")
            break outer
        }
        if pass == 3 {
            let stuck = startIndex == cur ? "，滚轮未生效" : ""
            print("定位未收敛：首行 AXIndex=\(cur)，目标 \(wanted)（起始首行 \(startIndex)\(stuck)；前台 \(frontmostName())）")
        }
    }
}

// 2) 按日期把视口滚到目标附近，再在附近**按名字**定位。
//
//    ⚠️ 日期只用来滚动，绝不允许改写 name。
//    旧实现把 name 覆盖成「该日期的第一行」，后果极其恶劣：
//      * 同一天有多个会话时（实测 4/2 是连续 5 行 1175~1179），5 个不同会话被劫持到同一行；
//      * 覆盖后 title 校验比对的是被覆盖的 name，自己改题自己判卷 → 必然通过，
//        于是「打开成功」被误判，后续会抓错人的消息还当成采到了（静默污染）；
//      * 列表漂移后序号指向别人（1193 已从「8000团队聚餐」变成「26年十周年IT联动」），
//        再叠加覆盖就彻底失去纠错能力。
//    名字是唯一身份，只能被拿来比对，不能被定位手段改写。
//    反过来，按名已经命中目标时日期提示就不再需要：目标收到新消息后旧日期标签可能已不在列表里，
//    硬要先找到旧日期会把可见的目标滚走并以 exit 11 失败。
if presetRow == nil && !name.isEmpty { presetRow = findRow() }
// 目标日期：优先用绝对日期 --seek-day；没有就退回把 --seek-date 文本还原。
// 行号会随新增/删除/收到新消息整体漂移，日期不会：列表按最后活跃倒序，日期单调不增，
// 所以用「日期差 → 行数」估算跳转距离，通常 2–4 跳即可落到目标日期块。
var targetDay = seekDay.isEmpty ? nil : dayOfISO(seekDay)
if targetDay == nil && !seekDate.isEmpty { targetDay = dayOfText(seekDate) }
if let target = targetDay, (presetRow == nil || name.isEmpty) {
    let seekStartIndex = firstIndex()
    var reached = false
    for _ in 1...80 {
        let rows = datedRows()
        guard let top = rows.min(by: { $0.0 < $1.0 }), let bottom = rows.max(by: { $0.0 < $1.0 }) else { break }
        // 命中：视口里有该日期，或视口正好跨过它
        if rows.contains(where: { $0.1 == target }) || (top.1 >= target && target >= bottom.1) {
            reached = true
            break
        }
        // 顶部是置顶块，日期乱序，插值只用「单调不增」的尾段作锚点
        let tail = monotonicTail(rows)
        let anchorTop = tail.first ?? top
        let anchorBottom = tail.last ?? bottom
        let goDown = anchorBottom.1 > target
        var lines = 24
        let rowSpan = anchorBottom.0 - anchorTop.0
        if rowSpan > 0 && anchorTop.1 != anchorBottom.1 {
            let perRow = abs(Double(anchorTop.1 - anchorBottom.1) / Double(rowSpan))
            if perRow > 0 {
                let gap = Double(abs((goDown ? anchorBottom.1 : anchorTop.1) - target))
                lines = Int(max(12.0, min(240.0, gap / perRow)))
            }
        }
        // 已经顶到头却还要往上，说明目标在下方，反向，否则原地空转
        if firstIndex() <= 1 && !goDown {
            wheel(-24, settleMs: 300)
            usleep(220000)
            continue
        }
        wheel(goDown ? -Int32(lines) : Int32(lines), settleMs: 300)
        usleep(220000)
    }
    guard reached else {
        let now = firstIndex()
        let stuck = now == seekStartIndex ? "，滚轮未生效" : ""
        fputs("按日期未定位到 \(seekDay.isEmpty ? seekDate : seekDay)（首行 AXIndex \(seekStartIndex)→\(now)\(stuck)；前台 \(frontmostName())）\n", stderr)
        exit(11)
    }
    print("目标日期 \(seekDay.isEmpty ? seekDate : seekDay) 已进入视口")

    if name.isEmpty {
        for r in visibleRows() where dayOfText(rowTime(r)) == target {
            presetRow = (r, rowIndex(r) ?? 0)
            print("按日期定位到：AXIndex \(presetRow!.1) \(rowNameOf(r))")
            break
        }
    } else {
        var probe = 0
        while presetRow == nil && probe < 40 {
            if let r = findRow() { presetRow = r; break }
            probe += 1
            let cur = datedRows().first?.1 ?? target
            wheel(cur > target ? -12 : 12, settleMs: 320)
            usleep(200000)
        }
        if let p = presetRow {
            print("按名定位到：AXIndex \(p.1) \(rowNameOf(p.0))")
        } else {
            print("日期附近未见「\(name)」，转为逐屏搜索")
        }
    }
}

// 1) 给了序号就先跳过去（序号会漂移，用 drift 累积补偿）


// 自动群名是「成员1、成员2、…」拼出来的。有人离职后群名会变短，
// 按旧名精确匹配必然失败。这里按成员集合做受控模糊匹配：
// 只接受「新名成员是旧名成员的子集」且共同成员足够多、占比过半的唯一候选。
func memberSet(_ n: String) -> Set<String> {
    Set(n.split(separator: "、").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
}
func fuzzyRow() -> (AXUIElement, Int)? {
    let want = memberSet(name)
    guard want.count >= 3 else { return nil }        // 非自动群名不参与
    var hits: [(AXUIElement, Int)] = []
    for r in visibleRows() {
        let got = memberSet(rowNameOf(r))
        guard got.count >= 2 else { continue }
        let common = want.intersection(got).count
        if common >= 2, Double(common) / Double(max(want.count, got.count)) >= 0.6 {
            hits.append((r, rowIndex(r) ?? 0))
        }
    }
    return hits.count == 1 ? hits[0] : nil           // 多个候选一律放弃，宁可失败也不开错
}

var hit = presetRow ?? findRow()
var screens = 0
while hit == nil && screens < maxScreens {
    let fromTop = indexHint <= 0 && seekDate.isEmpty
    let down = fromTop || screens % 2 == 0
    let steps = screens / 2 + 1
    for _ in 0..<(fromTop ? 1 : steps) { wheel(down ? -30 : 30, settleMs: 400) }
    usleep(400000)
    hit = findRow()
    screens += 1
}
if hit == nil { hit = fuzzyRow() }
guard let found = hit else {
    fputs("未找到会话「\(name)」（当前视口：\(visibleRows().map(rowNameOf))）\n", stderr); exit(7)
}
var row = found.0
let idx = found.1

// 按下前的最后一道确认：滚动过程中行元素会被复用，名字必须对得上。
// 对不上说明列表已漂移，就地重找而不是直接放弃——漂移是常态，放弃会让会话白白漏掉。
if !nameMatch(rowNameOf(row), name) {
    fputs("行已失效：AXIndex \(idx) 现在是「\(rowNameOf(row))」，期望「\(name)」，就地重找\n", stderr)
    var again: (AXUIElement, Int)?
    var retry = 0
    while again == nil && retry < maxScreens {
        wheel(retry % 2 == 0 ? -30 : 30, settleMs: 400)
        usleep(300000)
        again = findRow() ?? fuzzyRow()
        retry += 1
    }
    guard let g = again else { fputs("就地重找仍未命中「\(name)」\n", stderr); exit(13) }
    row = g.0
    print("就地重找命中：AXIndex \(g.1) \(rowNameOf(g.0))")
}

if !stopPrefix.isEmpty, rowTime(row).hasPrefix(stopPrefix) {
    print("BAND_END \(idx) \(rowTime(row)) \(rowNameOf(row))")
    exit(10)
}

func currentTitle() -> String? {
    requireStableLayout()
    let titleAreaStart = rf.minX + rf.width * 0.25
    let titleAreaBottom = rf.minY + rf.height * 0.08
    var out: [AXUIElement] = []
    func walkNodes(_ n: AXUIElement, _ d: Int) {
        if d > 12 { return }
        if let f = frameOf(n) {
            if f.maxX <= titleAreaStart { return }
            if f.minY >= titleAreaBottom { return }
        }
        out.append(n)
        if role(n) == "AXTable" { return }
        for c in children(n) { walkNodes(c, d + 1) }
    }
    walkNodes(root, 0)
    // 外部群横幅也落在标题区，必须排除，否则会读到「由企业微信用户创建的外部群…群主: xxx」
    let banner = ["由企业微信用户创建的外部群", "群主:"]
    return out.filter { (role($0) == "AXTextField" || role($0) == "AXStaticText") && !(string($0, kAXValueAttribute) ?? "").isEmpty }
        .filter { (centerOf($0)?.x ?? -1e9) >= titleAreaStart && (centerOf($0)?.y ?? 1e9) <= titleAreaBottom }
        .filter { v in let t = string(v, kAXValueAttribute) ?? ""; return !banner.contains { t.contains($0) } }
        .max(by: { (centerOf($0)?.x ?? 0) < (centerOf($1)?.x ?? 0) })
        .flatMap { string($0, kAXValueAttribute) }
}

/// 行是否被选中。注意：单聊右栏标题取的常是岗位/部门标签（实测「A5项目组员工」「棱镜工作室员工」
/// 「平台设计中心副总监」），不是对方姓名，所以标题不能作为"打开正确"的判据；改由选中行行名判定。
func rowSelected(_ r: AXUIElement) -> Bool {
    (attr(r, "AXSelected") as? NSNumber)?.boolValue ?? false
}

func matches(_ t: String?) -> Bool {
    guard let t, !t.isEmpty else { return false }
    return t == name || name.contains(t)
}

func waitForTitle(_ tries: Int, row: AXUIElement? = nil) -> String? {
    for _ in 0..<tries {
        if let t = currentTitle(), matches(t) { return t }
        if let row, rowSelected(row) { return currentTitle() }
        usleep(400000)
    }
    return currentTitle()
}

/// 「全局搜索 ⇧⌘F」是独立窗口（subrole=AXSystemDialog），会浮在会话列表上方抢走点击，
/// 表现为「名字定位正确、点下去却是上一会话的标题」。点行前先按 Esc 收掉。
func dismissOverlays() {
    guard let wins = attr(ae, kAXWindowsAttribute) as? [AXUIElement] else { return }
    let overlays = wins.filter { (attr($0, kAXSubroleAttribute) as? String) == "AXSystemDialog" }
    guard !overlays.isEmpty else { return }
    print("检测到 \(overlays.count) 个浮层窗口，先按 Esc 关闭")
    for _ in 0..<2 {
        let src = CGEventSource(stateID: .hidSystemState)
        CGEvent(keyboardEventSource: src, virtualKey: 53, keyDown: true)?.post(tap: .cghidEventTap)
        CGEvent(keyboardEventSource: src, virtualKey: 53, keyDown: false)?.post(tap: .cghidEventTap)
        usleep(300_000)
    }
}

var title: String?
var usedIdx = idx

func clickAt(_ p: CGPoint, expected: AXUIElement) -> Bool {
    ensureFrontmost()
    requireStableLayout()
    requireOnScreen(p)
    guard hitBelongsTo(expected, at: p) else { return false }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(60_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(80_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(600_000)
    return true
}

if debugGeo {
    print("[几何] 窗口 \(rf)")
    print("[几何] 列表 \(frameOf(table) ?? .zero)")
    if let raw = attr(table, kAXParentAttribute) {
        let parent = raw as! AXUIElement
        print("[几何] 列表父级 \(role(parent)) \(frameOf(parent) ?? .zero)")
    }
    let vr = visibleRows()
    for (pos, r) in vr.enumerated() {
        let f = frameOf(r) ?? .zero
        let mark = (rowIndex(r) == idx) ? "  <<< 目标" : ""
        print("[几何] 视口第 \(pos)/\(vr.count - 1) 行 AXIndex=\(rowIndex(r) ?? -1) y=\(Int(f.minY))…\(Int(f.maxY)) x=\(Int(f.minX))…\(Int(f.maxX)) \(rowNameOf(r).prefix(20))\(mark)")
    }
    if let f = frameOf(row) {
        print("[几何] 目标行 frame=\(f) center=\(CGPoint(x: f.midX, y: f.midY))")
        print("[几何] 目标行高度=\(Int(f.height))，视口首行 y=\(Int((frameOf(vr.first ?? row) ?? .zero).minY))，末行 y=\(Int((frameOf(vr.last ?? row) ?? .zero).maxY))")
    } else {
        print("[几何] 目标行读不到 frame（元素已失效）")
    }
}

/// 当前被选中的行。列表重排后行元素会被复用，「目标元素是否被选中」不如「现在被选中的是谁」可靠。
func selectedRow() -> AXUIElement? {
    for r in visibleRows() where rowSelected(r) { return r }
    return nil
}

func selectedRowName() -> String? {
    guard let s = selectedRow() else { return nil }
    let n = rowNameOf(s)
    return n.isEmpty ? nil : n
}

// Geometric clipping keeps genuine first/last rows; AX hit testing rejects preloaded blank rows.
func rowClickPoint(_ rowFrame: CGRect, _ viewport: CGRect, _ window: CGRect) -> CGPoint? {
    let area = rowFrame.intersection(viewport).intersection(window)
    guard !area.isNull, area.width > 8, area.height >= 8 else { return nil }
    return CGPoint(x: area.midX, y: area.midY)
}
func hitBelongsTo(_ expected: AXUIElement, at point: CGPoint) -> Bool {
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit) == .success,
          var current = hit else { return false }
    for _ in 0..<12 {
        if CFEqual(current, expected) { return true }
        guard let raw = attr(current, kAXParentAttribute) else { return false }
        current = raw as! AXUIElement
    }
    return false
}
/// 诊断用：某个屏幕点当前命中的是谁（所属应用、角色、文字前缀）。
/// 命中测试拒绝点击时，只有知道「挡在那里的是什么」，才能区分遮挡、失焦和行元素复用。
func describeHit(at point: CGPoint) -> String {
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit) == .success,
          let h = hit else { return "无命中元素" }
    var pid: pid_t = 0
    _ = AXUIElementGetPid(h, &pid)
    let owner = pid == app.processIdentifier ? "企业微信"
        : (NSRunningApplication(processIdentifier: pid)?.localizedName ?? "pid \(pid)")
    let sub = string(h, kAXSubroleAttribute).map { "/\($0)" } ?? ""
    let text = (string(h, kAXValueAttribute) ?? string(h, kAXTitleAttribute) ?? "").prefix(20)
    return "\(owner) \(role(h))\(sub)「\(text)」"
}
var lastPointIssue = ""
/// 命中元素所属的会话行（向上找 AXRow）。
func ownerRow(_ e: AXUIElement) -> AXUIElement? {
    var current = e
    for _ in 0..<12 {
        if role(current) == "AXRow" { return current }
        guard let raw = attr(current, kAXParentAttribute) else { return nil }
        current = raw as! AXUIElement
    }
    return nil
}
/// 按「屏幕上实际画的是谁」找目标行：在侧栏视口内竖向采样命中测试，把命中元素归到所属行，再按名字匹配。
/// 用于 AX 报告的行坐标与实际渲染不一致的情况（实测目标行 frame 处命中的是另一行的消息预览）。
/// 只返回唯一匹配；全等优先，否则要求截断匹配唯一，歧义一律放弃，不猜。
func screenRow(named target: String, viewport vp: CGRect) -> (AXUIElement, CGPoint)? {
    var hits: [(row: AXUIElement, rowName: String, points: [CGPoint])] = []
    var y = vp.minY + 4
    while y < vp.maxY - 4 {
        let p = CGPoint(x: vp.midX, y: y)
        var hit: AXUIElement?
        if AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(p.x), Float(p.y), &hit) == .success,
           let h = hit, let row = ownerRow(h) {
            if let i = hits.firstIndex(where: { CFEqual($0.row, row) }) {
                hits[i].points.append(p)
            } else {
                hits.append((row, rowNameOf(row), [p]))
            }
        }
        y += 8
    }
    let exact = hits.filter { $0.rowName == target }
    let matched = exact.isEmpty ? hits.filter { nameMatch($0.rowName, target) } : exact
    guard matched.count == 1, let only = matched.first else { return nil }
    return (only.row, only.points[only.points.count / 2])
}
func actionablePoint(_ target: AXUIElement) -> CGPoint? {
    requireStableLayout()
    guard let f = frameOf(target), let vp = frameOf(pick.scroll), let wf = frameOf(root) else {
        lastPointIssue = "目标行或侧栏读不到 frame"; return nil
    }
    guard let p = rowClickPoint(f, vp, wf) else {
        lastPointIssue = "目标行与侧栏视口交集不足（行 y=\(Int(f.minY))…\(Int(f.maxY))，视口 y=\(Int(vp.minY))…\(Int(vp.maxY))）"
        return nil
    }
    guard onAwakeDisplay(p) else { lastPointIssue = "点击点 (\(Int(p.x)),\(Int(p.y))) 不在唤醒的显示器上"; return nil }
    guard hitBelongsTo(target, at: p) else {
        lastPointIssue = "点击点 (\(Int(p.x)),\(Int(p.y))) 命中的是 \(describeHit(at: p))"; return nil
    }
    return p
}
var opened = false
for attempt in 1...3 {
    dismissOverlays()
    // 过期提示也可能是在上一个会话「读历史」的途中才弹出的，只靠脚本开头那一次清不掉。
    // 每轮点击尝试前再清一次，避免它一直盖着列表导致后续全部点不动。
    resolveDialogsOrExit()
    // 关键：按 Esc 收浮层后会话列表会失焦，紧接着的滚轮事件会全部失效，
    // 表现为「目标行滚不到位 → 尚不可点击 → 连续失败停轮」。
    // 必须在滚动之前把企业微信重新拉回前台并给它一点时间恢复。
    ensureFrontmost()
    usleep(250_000)
    // Never reuse an element after a scroll or overlay dismissal.
    var target = findRow()?.0
    var point = target.flatMap(actionablePoint)
    for _ in 0..<6 {
        if point != nil { break }
        requireStableLayout()
        guard let current = target, let f = frameOf(current) else { break }
        let before = f
        wheel(f.midY < viewport.midY ? 12 : -12, settleMs: 400)
        target = findRow()?.0
        point = target.flatMap(actionablePoint)
        if point == nil, let next = target, frameOf(next) == before { break }
    }
    if point == nil, target != nil, let vp = frameOf(pick.scroll),
       let (row, p) = screenRow(named: name, viewport: vp), onAwakeDisplay(p) {
        print("第 \(attempt) 次：目标行坐标与实际渲染不一致（\(lastPointIssue)），按屏幕命中改用行「\(rowNameOf(row))」(\(Int(p.x)),\(Int(p.y)))")
        target = row
        point = p
    }
    guard let target, let point else {
        let why = target == nil ? "滚动后按名未再找到目标行" : lastPointIssue
        print("第 \(attempt) 次目标行尚不可点击（\(why)；前台 \(frontmostName())），保留候选但不点击空白/遮挡区域")
        continue
    }
    guard clickAt(point, expected: target) else {
        print("第 \(attempt) 次点击前复核未通过（点击点命中 \(describeHit(at: point))；前台 \(frontmostName())），未点击")
        continue
    }
    _ = waitForTitle(8, row: target)
    let selName = selectedRowName()
    title = currentTitle()
    if let s = selectedRow() { usedIdx = rowIndex(s) ?? usedIdx }
    if (selName.map { nameMatch($0, name) } ?? false) || matches(title) { opened = true; break }
    print("第 \(attempt) 次打开未命中（选中行「\(selName ?? "<无>")」，标题「\(title ?? "<无>")」；点击点现在命中 \(describeHit(at: point))，前台 \(frontmostName())），重新定位再试")
    usleep(500_000)
}

guard opened else {
    fputs("会话校验失败：期望 \(name)，选中行「\(selectedRowName() ?? "<无>")」，标题「\(title ?? "<无>")」\n", stderr)
    exit(8)
}
// 打开会话后企业微信会刷新自动拼接的群名（去掉离职成员），稍等再读侧栏选中行。
// group(2) 交给上层登记别名：必须用侧栏行名（按字号取），不能用主区 title（常是岗位/部门标签）。
usleep(100_000)
let refreshedName = selectedRowName() ?? title ?? ""
print("OPENED \(usedIdx) \(refreshedName)")
exit(0)
