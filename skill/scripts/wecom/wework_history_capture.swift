import Cocoa
import ApplicationServices
import CryptoKit
import Foundation
import ScreenCaptureKit
// 输出重定向到日志文件时默认全缓冲（退出才落盘），实时控制台与卡住检测就看不到进展；改为逐行刷新。
_ = setvbuf(stdout, nil, _IOLBF, 0)

// 原生「查找聊天记录」采集器。只读消息；仅打开/关闭面板和滚动。
struct Options {
    var conversation = "", prefix = "wework-history", since = "", type = "unknown", replaySnapshots = ""
    var selfNames: [String] = []
    var maxScrolls = 1000, idleLimit = 6, settleMs = 800, checkpointEvery = 10
    var delta: Int32 = 120
    var noScroll = false, closePanel = false, mainLatest = false, selfTest = false, captureInlineImages = false, copyInlineText = true, probeVisible = false
}
func options() -> Options {
    var c = Options(), args = Array(CommandLine.arguments.dropFirst())
    while !args.isEmpty {
        let key = args.removeFirst()
        switch key {
        case "--no-scroll": c.noScroll = true
        case "--close-panel": c.closePanel = true
        case "--main-latest": c.mainLatest = true
        case "--self-test": c.selfTest = true
        case "--probe-visible": c.probeVisible = true
        case "--capture-inline-images": c.captureInlineImages = true
        case "--no-copy-inline-text": c.copyInlineText = false
        default:
            guard !args.isEmpty else { fputs("缺少参数值\n", stderr); exit(64) }
            let value = args.removeFirst()
            switch key {
            case "--conversation": c.conversation = value
            case "--output-prefix": c.prefix = value
            case "--since": c.since = value
            case "--conversation-type": c.type = value
            case "--replay-snapshots": c.replaySnapshots = value
            case "--self-names": c.selfNames = value.split(separator: ",").map(String.init)
            case "--max-scrolls": c.maxScrolls = max(1, min(Int(value) ?? 1000, 100000))
            case "--idle-limit": c.idleLimit = max(1, min(Int(value) ?? 6, 50))
            case "--settle-ms": c.settleMs = max(100, min(Int(value) ?? 800, 10000))
            case "--checkpoint-every": c.checkpointEvery = max(1, Int(value) ?? 10)
            case "--scroll-delta": c.delta = Int32(value) ?? 120
            case "--year": break // 与主聊天区采集器参数兼容；日期采用完整 --since。
            default: fputs("未知参数：\(key)\n", stderr); exit(64)
            }
        }
    }
    return c
}
func attr(_ e: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, name as CFString, &value) == .success else { return nil }
    return value as AnyObject?
}
func string(_ e: AXUIElement, _ name: String) -> String? {
    let value = attr(e, name)
    return (value as? String) ?? (value as? NSAttributedString)?.string
}
func children(_ e: AXUIElement) -> [AXUIElement] { attr(e, kAXChildrenAttribute) as? [AXUIElement] ?? [] }
func frame(_ e: AXUIElement) -> CGRect? {
    guard let p = attr(e, kAXPositionAttribute), let s = attr(e, kAXSizeAttribute) else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point), AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
func onAwakeDisplay(_ point: CGPoint) -> Bool {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return false }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return false }
    return ids.prefix(Int(count)).contains { CGDisplayIsAsleep($0) == 0 && CGDisplayBounds($0).contains(point) }
}
struct Node {
    let element: AXUIElement
    let role: String
    let value: String?
    let title: String?
    let childElements: [AXUIElement]
}
let attributes = [kAXRoleAttribute, kAXValueAttribute, kAXTitleAttribute, kAXChildrenAttribute] as CFArray
func read(_ e: AXUIElement) -> Node {
    var values: CFArray?
    guard AXUIElementCopyMultipleAttributeValues(e, attributes, AXCopyMultipleAttributeOptions(rawValue: 0), &values) == .success,
          let a = values as? [AnyObject], a.count == 4 else {
        return Node(element: e, role: "", value: nil, title: nil, childElements: [])
    }
    func text(_ v: AnyObject) -> String? { (v as? String) ?? (v as? NSAttributedString)?.string }
    return Node(element: e, role: text(a[0]) ?? "", value: text(a[1]), title: text(a[2]), childElements: a[3] as? [AXUIElement] ?? [])
}
func walk(_ e: AXUIElement) -> [Node] { let node = read(e); return [node] + node.childElements.flatMap(walk) }
func person(_ name: String) -> String {
    let base = name.components(separatedBy: "@").first ?? name
    guard let range = base.range(of: #"(?<=[（(])[^（）()]+(?=[）)]$)"#, options: .regularExpression) else { return base }
    return String(base[range])
}
let timePattern = #"^(?:(?:(?:19|20)\d{2}[/年-]|\d{2}/)?\d{1,2}[/月-]\d{1,2}日?)\s+\d{1,2}:\d{2}(?=$|\s)"#
func strictDate(_ year: Int, _ month: Int, _ day: Int) -> Date? {
    let cal = Calendar(identifier: .gregorian)
    guard let date = cal.date(from: DateComponents(year: year, month: month, day: day)) else { return nil }
    let actual = cal.dateComponents([.year,.month,.day],from:date)
    return actual.year == year && actual.month == month && actual.day == day ? date : nil
}
func timestamp(_ text: String) -> String? {
    guard let range = text.range(of: timePattern, options: .regularExpression) else { return nil }
    let label = String(text[range]), n = label.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
    guard n.count == 4 || n.count == 5 else { return nil }
    let offset = n.count == 5 ? 1 : 0
    let year = offset == 1 ? (n[0] < 100 ? 2000 + n[0] : n[0]) : 2000
    guard strictDate(year,n[offset],n[offset+1]) != nil,
          (0...23).contains(n[offset+2]), (0...59).contains(n[offset+3]) else { return nil }
    return label
}
func recordDate(_ row: [String: Any], relativeTo now: Date) -> Date? {
    guard let raw = row["time_text"] as? String, let text = timestamp(raw) else { return nil }
    let numbers = text.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
    let cal = Calendar(identifier: .gregorian)
    if numbers.count == 5 { return strictDate(numbers[0] < 100 ? 2000 + numbers[0] : numbers[0], numbers[1], numbers[2]) }
    guard numbers.count >= 4 else { return nil }
    var year = cal.component(.year, from: now)
    guard let candidate = strictDate(year,numbers[0],numbers[1]) else { return nil }
    if candidate > cal.startOfDay(for: now) { year -= 1 }
    return strictDate(year,numbers[0],numbers[1])
}
func renderAttachments(_ text: String, labels: [Int: String] = [:]) -> (String, [[String: Any]]) {
    let ns = text as NSString
    var rendered = "", attachments: [[String: Any]] = []
    var start = 0
    for i in 0..<ns.length where ns.character(at: i) == 0xfffc {
        rendered += ns.substring(with: NSRange(location: start, length: i - start))
        let label = labels[i]
        rendered += label.map { "[\($0)]" } ?? "[内嵌表情/附件：未识别]"
        attachments.append(["kind": "inline_attachment", "offset_utf16": i,
                            "label": label as Any? ?? NSNull(), "identified": label != nil,
                            "label_source": label == nil ? "unavailable" : "accessibility_attachment"])
        start = i + 1
    }
    rendered += ns.substring(from: start)
    return (rendered, attachments)
}
// 除了附件占位之外，复制得到的文本必须与 AX 原文逐字一致。
func copiedLabels(_ raw: String, _ copied: String) -> [Int: String]? {
    let parts = raw.components(separatedBy: "\u{fffc}")
    guard parts.count > 1 else { return nil }
    var pattern = "\\A" + NSRegularExpression.escapedPattern(for: parts[0]), offsets: [Int] = []
    var position = (parts[0] as NSString).length
    for part in parts.dropFirst() {
        offsets.append(position)
        pattern += #"\[([^\[\]\r\n]{1,40})\]"# + NSRegularExpression.escapedPattern(for: part)
        position += 1 + (part as NSString).length
    }
    pattern += "\\z"
    guard let regex = try? NSRegularExpression(pattern: pattern),
          let match = regex.firstMatch(in: copied, range: NSRange(location: 0, length: (copied as NSString).length)),
          match.numberOfRanges == offsets.count + 1 else { return nil }
    let ns = copied as NSString
    return Dictionary(uniqueKeysWithValues: offsets.enumerated().map { ($0.element, ns.substring(with: match.range(at: $0.offset + 1))) })
}
func attachmentLabels(_ node: Node) -> [Int: String] {
    guard let text = node.value, text.contains("\u{fffc}") else { return [:] }
    var range = CFRange(location: 0, length: (text as NSString).length)
    guard let parameter = AXValueCreate(.cfRange, &range) else { return [:] }
    var result: CFTypeRef?
    guard AXUIElementCopyParameterizedAttributeValue(node.element, kAXAttributedStringForRangeParameterizedAttribute as CFString, parameter, &result) == .success,
          let rich = result as? NSAttributedString else { return [:] }
    var labels: [Int: String] = [:]
    rich.enumerateAttribute(NSAttributedString.Key("AXAttachment"), in: NSRange(location: 0, length: rich.length)) { value, span, _ in
        guard let value, CFGetTypeID(value as CFTypeRef) == AXUIElementGetTypeID() else { return }
        let element = value as! AXUIElement
        let label = [kAXDescriptionAttribute, kAXTitleAttribute, kAXHelpAttribute].compactMap { string(element, $0) }.first { !$0.isEmpty }
        if let label, label.count <= 80 { labels[span.location] = label }
    }
    return labels
}
func rowKey(_ row: [String: Any]) -> String {
    let parts: [Any] = [row["sender"] ?? NSNull(), row["time_text"] ?? NSNull(), row["body_raw"] ?? "", row["links"] ?? []]
    let data = try! JSONSerialization.data(withJSONObject: parts, options: .sortedKeys)
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}
func mergeSnapshot(_ current: [[String: Any]], _ previous: [[String: Any]]) -> [[String: Any]]? {
    if previous.isEmpty { return current }
    let a = current.map(rowKey), b = previous.map(rowKey), limit = min(current.count, previous.count)
    guard limit > 0 else { return nil }
    func isDelayedBody(_ fresh: [String: Any], _ old: [String: Any]) -> Bool {
        guard fresh["sender"] as? String == old["sender"] as? String,
              fresh["time_text"] as? String == old["time_text"] as? String else { return false }
        let x = fresh["body_raw"] as? String ?? "", y = old["body_raw"] as? String ?? ""
        let empty = x.isEmpty ? fresh : old
        return x != y && (x.isEmpty || y.isEmpty) &&
            ((empty["links"] as? [String]) ?? []).isEmpty
    }
    for n in stride(from: limit, through: 1, by: -1) {
        let newKeys = Array(a.suffix(n)), oldKeys = Array(b.prefix(n))
        let different = (0..<n).filter { newKeys[$0] != oldKeys[$0] }
        let anchors = [0] + different.map { $0 + 1 } + [n + 1]
        let allDelayed = !different.isEmpty &&
            different.allSatisfy { isDelayedBody(current[current.count - n + $0], previous[$0]) }
        let spaced = different.count <= max(3, n / 20) && n >= 5 &&
            zip(anchors, anchors.dropFirst()).allSatisfy { $1 - $0 >= 3 }
        // 延迟渲染行最常落在重叠区边缘（上一屏最旧端尚未渲染），夹不出两侧锚点；
        // 其余位置有 ≥2 条不同的精确匹配行时同样能确认对齐。
        let exactKeys = Set((0..<n).filter { !different.contains($0) }.map { newKeys[$0] })
        let anchored = n >= 3 && different.count <= max(5, n / 4) && exactKeys.count >= 2
        let delayed = allDelayed && (spaced || anchored)
        if different.isEmpty || delayed {
            var enriched = previous
            for i in 0..<n {
                let freshRow = current[current.count - n + i]
                if delayed && different.contains(i) &&
                    ((freshRow["body_raw"] as? String ?? "").count > (enriched[i]["body_raw"] as? String ?? "").count) {
                    enriched[i] = freshRow
                }
                let newer = current[current.count - n + i]["attachments"] as? [[String: Any]] ?? []
                var older = previous[i]["attachments"] as? [[String: Any]] ?? []
                for j in older.indices where older[j]["image_path"] == nil {
                    if let found = newer.first(where: { ($0["offset_utf16"] as? Int) == (older[j]["offset_utf16"] as? Int) && $0["image_path"] != nil }) {
                        for field in ["image_path", "image_source", "visual_only"] { older[j][field] = found[field] }
                    }
                }
                enriched[i]["attachments"] = older
            }
            return Array(current.dropLast(n)) + enriched
        }
    }
    return nil
}
let config = options()
if !config.replaySnapshots.isEmpty {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: config.replaySnapshots)),
          let source = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let previous = source["records"] as? [[String: Any]],
          let current = source["unmerged_records"] as? [[String: Any]],
          let merged = mergeSnapshot(current, previous) else {
        fputs("REPLAY_MERGE_GAP\n", stderr); exit(1)
    }
    print("REPLAY_MERGE_OK \(merged.count)"); exit(0)
}
if config.selfTest {
    let dateFormatter = DateFormatter()
    dateFormatter.calendar = Calendar(identifier:.gregorian); dateFormatter.locale = Locale(identifier:"en_US_POSIX")
    dateFormatter.dateFormat = "yyyy-MM-dd"
    let reference = dateFormatter.date(from:"2026-10-27")!
    guard timestamp("24/11/25 10:47 引用者:") == "24/11/25 10:47",
          recordDate(["time_text":"24/11/25 10:47"],relativeTo:reference) == dateFormatter.date(from:"2024-11-25"),
          recordDate(["time_text":"25/12/31 23:59"],relativeTo:reference) == dateFormatter.date(from:"2025-12-31"),
          timestamp("25/2/29 10:00") == nil, timestamp("24/2/29 24:00") == nil,
          recordDate(["time_text":"24/2/29 10:00"],relativeTo:reference) == dateFormatter.date(from:"2024-02-29") else { exit(70) }
    let r = renderAttachments("甲👍🏻\u{fffc}乙", labels: [5: "强"])
    let unknown = renderAttachments("\u{fffc}")
    let a: [String: Any] = ["sender": "甲", "time_text": "10/27 12:00", "body_raw": "收到"]
    let stable = (0..<5).map { i -> [String: Any] in
        ["sender": "甲", "time_text": "10/27 12:0\(i)", "body_raw": "第\(i)条", "links": [String]()]
    }
    var delayed = stable
    delayed[2]["body_raw"] = ""
    var twoDelayed = (0..<9).map { i -> [String: Any] in
        ["sender": "甲", "time_text": "10/27 13:0\(i)", "body_raw": "第\(i)条", "links": [String]()]
    }
    twoDelayed[2]["body_raw"] = ""; twoDelayed[6]["body_raw"] = ""
    var twoFull = twoDelayed
    twoFull[2]["body_raw"] = "补齐甲"; twoFull[2]["links"] = ["链接"]
    twoFull[6]["body_raw"] = "补齐乙"
    var contradictory = stable
    contradictory[2]["body_raw"] = "另一条"
    guard renderAttachments("👍🏻🙂").0 == "👍🏻🙂", r.0.contains("👍🏻"),
          r.0 == "甲👍🏻[强]乙", unknown.0 == "[内嵌表情/附件：未识别]", unknown.1.count == 1,
          copiedLabels("甲👍🏻\u{fffc}\u{fffc}乙", "甲👍🏻[强][抱拳]乙") == [5:"强", 6:"抱拳"],
          copiedLabels("甲\u{fffc}乙", "甲[强]丙") == nil,
          timestamp("10/27 13:17 被引用者:") == "10/27 13:17",
          person("alias(中文)@企业") == "中文",
          mergeSnapshot([a, a], [])?.count == 2,
          mergeSnapshot([a, a], [a, a])?.count == 2,
          mergeSnapshot(stable, delayed)?[2]["body_raw"] as? String == "第2条",
          mergeSnapshot(twoFull, twoDelayed)?[2]["body_raw"] as? String == "补齐甲",
          mergeSnapshot(twoFull, twoDelayed)?[6]["body_raw"] as? String == "补齐乙",
          mergeSnapshot(contradictory, stable) == nil else { exit(70) }
    print("HISTORY_SELF_TEST_OK"); exit(0)
}
setbuf(stdout, nil)
guard !config.conversation.isEmpty, AXIsProcessTrusted(),
      let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.WeWorkMac").first else {
    fputs("缺少会话名称、辅助功能权限或企业微信进程\n", stderr); exit(2)
}
let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
guard (session["CGSSessionScreenIsLocked"] as? Int ?? 0) == 0 else {
    fputs("屏幕已锁定\n", stderr); exit(20)
}
let ae = AXUIElementCreateApplication(app.processIdentifier)
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
guard let focused = attr(ae, kAXFocusedWindowAttribute) else { exit(3) }
let root = focused as! AXUIElement
guard let windowFrame = frame(root), windowFrame.width > 600 else { fputs("当前不是企业微信主窗口\n", stderr); exit(4) }
var tables: [(AXUIElement, AXUIElement)] = [], headers: [Node] = []
func scan(_ element: AXUIElement, scroll: AXUIElement? = nil) {
    let node = read(element)
    let currentScroll = node.role == "AXScrollArea" ? element : scroll
    if node.role == "AXTable" { tables.append((element, currentScroll ?? element)); return }
    headers.append(node)
    for child in node.childElements { scan(child, scroll: currentScroll) }
}
func refresh() { tables = []; headers = []; scan(root) }
func sidebarTable() -> AXUIElement? {
    return tables.filter({ pair in
        guard let f = frame(pair.0) else { return false }
        return f.width > 180 && f.width < 500 && f.minX < windowFrame.minX + windowFrame.width * 0.4
    }).min(by: { (frame($0.0)?.minX ?? 0) < (frame($1.0)?.minX ?? 0) })?.0
}
func selectedConversation() -> String? {
    guard let table = sidebarTable() else { return nil }
    let rows = attr(table, "AXVisibleRows") as? [AXUIElement] ?? children(table)
    for row in rows where (attr(row, kAXSelectedAttribute) as? Bool) == true {
        return walk(row).first { $0.role == "AXStaticText" && !($0.value ?? "").isEmpty }?.value
    }
    return nil
}
func memberSet(_ n: String) -> Set<String> {
    Set(n.split(separator: "、").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
}
func renamedTo(_ name: String) -> Bool {
    // 自动群名由成员拼接，有人离职后名字变短；侧栏常常要打开后才刷新成新名。
    // 只接受「当前名是请求名的成员子集」且共同成员过半，避免把别的群当成它。
    let want = memberSet(config.conversation), got = memberSet(name)
    guard want.count >= 3, got.count >= 2 else { return false }
    // 自动群名会随成员进出变化：不能只接受「变短」，也要接受「走了几个、又来了几个」。
    // 用共同成员占比判定，阈值 0.6；调用方还要求候选唯一，双重约束下才落点。
    let common = want.intersection(got).count
    guard common >= 2 else { return false }   // 3 人小群少 1 人也必须能匹配
    return Double(common) / Double(max(want.count, got.count)) >= 0.6
}
func nameMatches(_ name: String?) -> Bool {
    guard let name else { return false }
    if name == config.conversation { return true }
    if name.hasSuffix("…") { return config.conversation.hasPrefix(String(name.dropLast())) }
    return renamedTo(name)
}
func click(_ element: AXUIElement) -> Bool {
    guard let f = frame(element), f.width > 0, f.height > 0 else { return false }
    let point = CGPoint(x: f.midX, y: f.midY)
    guard onAwakeDisplay(point) else {
        fputs("记录面板点击位置已移出所有唤醒的活动显示器\n", stderr); exit(22)
    }
    app.activate(options: [.activateAllWindows])
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(50_000)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(300_000)
    return true
}
func closeHistory() {
    refresh()
    if let button = headers.first(where: { $0.role == "AXButton" && ($0.title == "关闭聊天记录" || string($0.element, kAXHelpAttribute) == "关闭聊天记录") }) { _ = click(button.element) }
}
func latest(_ scroll: AXUIElement) -> Bool {
    let bars = children(scroll).filter { string($0, kAXRoleAttribute) == "AXScrollBar" && (string($0, kAXOrientationAttribute) ?? "") != "AXHorizontalOrientation" }
    guard let bar = bars.first else { return false }
    if (attr(bar, kAXValueAttribute) as? NSNumber)?.doubleValue ?? 0 >= 0.999 { return true }
    let code = AXUIElementSetAttributeValue(bar, kAXValueAttribute as CFString, NSNumber(value: 1))
    usleep(200_000)
    return code == .success && ((attr(bar, kAXValueAttribute) as? NSNumber)?.doubleValue ?? 0) >= 0.999
}
func verticalScrollValue(_ scroll: AXUIElement) -> Double? {
    guard let bar = children(scroll).first(where: {
        string($0, kAXRoleAttribute) == "AXScrollBar" &&
        (string($0, kAXOrientationAttribute) ?? "") != "AXHorizontalOrientation"
    }) else { return nil }
    return (attr(bar, kAXValueAttribute) as? NSNumber)?.doubleValue
}
refresh()
if case .blocked(let why) = handleDialogs() { fputs("弹窗阻塞本会话：\(why)\n", stderr); exit(25) }
refresh()
guard let current = selectedConversation(), nameMatches(current) else {
    fputs("当前选中会话与请求不符，停止\n", stderr); exit(8)
}
if current != config.conversation {
    // 告知上层：这个会话已经改名（成员减少），把新名登记为别名
    print("RENAMED \(current)")
}
if config.closePanel {
    closeHistory()
    if config.mainLatest {
        refresh()
        guard let sidebar = sidebarTable(), let sideFrame = frame(sidebar),
              let main = tables.filter({ pair in
                  guard let f = frame(pair.0) else { return false }
                  return f.minX >= sideFrame.maxX - 3 && f.width > 250
              }).min(by: { (frame($0.0)?.minX ?? 0) < (frame($1.0)?.minX ?? 0) }), latest(main.1) else {
            fputs("无法确认主聊天区最新端，跳过补证\n", stderr); exit(9)
        }
    }
    exit(0)
}
// 每次重新打开，避免沿用上一次会话的筛选或滚动状态。
closeHistory(); refresh()
guard let open = headers.first(where: { $0.role == "AXButton" && ($0.title == "查找聊天记录" || string($0.element, kAXHelpAttribute) == "查找聊天记录") }), click(open.element) else {
    fputs("无法打开聊天记录面板\n", stderr); exit(5)
}
var opened = false
for _ in 0..<12 {
    refresh()
    if headers.contains(where: { $0.value == "查找聊天记录" }) { opened = true; break }
    usleep(150_000)
}
guard opened, nameMatches(selectedConversation()), let history = tables.max(by: { (frame($0.0)?.minX ?? 0) < (frame($1.0)?.minX ?? 0) }) else { exit(6) }
defer { closeHistory() }
let historyX = frame(history.0)?.minX ?? windowFrame.maxX
guard !headers.contains(where: { node in
    node.role == "AXTextField" && (frame(node.element)?.minX ?? 0) >= historyX && !(node.value ?? "").isEmpty
}) else { fputs("记录面板带有搜索条件，拒绝按全量导出\n", stderr); exit(10) }
guard latest(history.1) else { fputs("无法确认记录面板位于最新端\n", stderr); exit(9) }
if config.probeVisible {
    let all = children(history.0).filter { string($0, kAXRoleAttribute) == "AXRow" }
    let visible = attr(history.0, kAXVisibleRowsAttribute) as? [AXUIElement] ?? []
    print("VISIBLE_PROBE all=\(all.count) visible=\(visible.count)")
    exit(0)
}
let reference = Date(), formatter = DateFormatter()
formatter.locale = Locale(identifier: "en_US_POSIX"); formatter.dateFormat = "yyyy-MM-dd"
guard let since = formatter.date(from: config.since) else { fputs("需要 --since YYYY-MM-DD\n", stderr); exit(64) }
let selfNames = Set(config.selfNames.flatMap { [$0, person($0)] } + ["我"])
var clipboardDisabled = false, clipboardDisableReason: String? = nil
var copyAttempts = 0, copySuccesses = 0, selectionMoved = false, ambiguousSkipped = 0
func sameRow(_ element: AXUIElement, _ expected: [String: Any]) -> Bool {
    let nodes = walk(element)
    let sender = nodes.first { $0.role == "AXButton" && !($0.title ?? "").isEmpty && $0.title != "按钮" }?.title
    let time = nodes.filter { $0.role == "AXStaticText" }.compactMap { $0.value.flatMap(timestamp) }.first
    let raw = nodes.filter { $0.role == "AXTextArea" && !($0.value ?? "").isEmpty }.compactMap(\.value).joined(separator: "\n---\n")
    return sender == (expected["sender"] as? String) && time == (expected["time_text"] as? String) && raw == (expected["body_raw"] as? String)
}
func inlineTextFromClipboard(_ node: Node, row: AXUIElement, expected: [String: Any]) -> [Int: String]? {
    guard config.copyInlineText, !clipboardDisabled, let raw = node.value, raw.contains("\u{fffc}"),
          NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else { return nil }
    guard nameMatches(selectedConversation()), sameRow(row, expected) else { selectionMoved = true; return nil }
    let pb = NSPasteboard.general, before = pb.changeCount
    var saved: [NSPasteboardItem] = []
    for item in pb.pasteboardItems ?? [] {
        let clone = NSPasteboardItem()
        for type in item.types {
            guard let data = item.data(forType: type), clone.setData(data, forType: type) else {
                clipboardDisabled = true; clipboardDisableReason = "backup_unavailable"; return nil
            }
        }
        saved.append(clone)
    }
    guard pb.changeCount == before else { clipboardDisabled = true; clipboardDisableReason = "clipboard_changed_during_backup"; return nil }
    let bars = children(history.1).filter { string($0,kAXRoleAttribute) == "AXScrollBar" && string($0,kAXOrientationAttribute) == "AXVerticalOrientation" }
    let scrollBefore = bars.first.flatMap { attr($0,kAXValueAttribute) as? NSNumber }?.doubleValue
    var span = CFRange(location: 0, length: (raw as NSString).length)
    guard AXUIElementSetAttributeValue(node.element,kAXFocusedAttribute as CFString,kCFBooleanTrue) == .success,
          let range = AXValueCreate(.cfRange,&span),
          AXUIElementSetAttributeValue(node.element,kAXSelectedTextRangeAttribute as CFString,range) == .success else { return nil }
    let scrollAfter = bars.first.flatMap { attr($0,kAXValueAttribute) as? NSNumber }?.doubleValue
    if let a=scrollBefore,let b=scrollAfter,abs(a-b)>0.00001 { selectionMoved = true; return nil }
    guard string(node.element,kAXSelectedTextAttribute) == raw,
          string(node.element,kAXValueAttribute) == raw, pb.changeCount == before,
          NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
          let menuBar = attr(ae,kAXMenuBarAttribute),
          let edit = children(menuBar as! AXUIElement).first(where: { ["编辑","Edit"].contains(string($0,kAXTitleAttribute) ?? "") }),
          AXUIElementPerformAction(edit,kAXPressAction as CFString) == .success else { return nil }
    usleep(60_000)
    guard let item=walk(edit).first(where:{ $0.role == "AXMenuItem" && ["复制","Copy"].contains($0.title ?? "") && (attr($0.element,kAXEnabledAttribute) as? Bool) == true }),
          pb.changeCount == before else { _ = AXUIElementPerformAction(edit,kAXCancelAction as CFString); return nil }
    copyAttempts += 1
    guard AXUIElementPerformAction(item.element,kAXPressAction as CFString) == .success else { return nil }
    var after = before, labels: [Int: String]? = nil, ready = false
    // 企微可能先清空、再写入剪贴板；等到内容可识别且版本稳定再读取。
    for _ in 0..<80 {
        let candidate = pb.changeCount
        if candidate != before {
            let copied = pb.string(forType: .string)
            let proposed = copied.flatMap { copiedLabels(raw, $0) }
            let isWeCom = (pb.types ?? []).contains { $0.rawValue == "WWKPrivatePBDataKey_Rich" }
            if (isWeCom || proposed != nil), pb.changeCount == candidate {
                usleep(proposed == nil ? 200_000 : 50_000)
                if pb.changeCount == candidate { after = candidate; labels = proposed; ready = true; break }
            }
        }
        usleep(25_000)
    }
    guard ready else { clipboardDisabled = true; clipboardDisableReason = "copy_not_verified"; return nil }
    // 验证剪贴板仍是刚读取的版本，才恢复之前的所有内容，避免覆盖用户的新复制。
    if pb.changeCount == after {
        pb.clearContents()
        if !saved.isEmpty && !pb.writeObjects(saved) { clipboardDisabled = true; clipboardDisableReason = "restore_failed" }
    } else { clipboardDisabled = true; clipboardDisableReason = "clipboard_changed_before_restore"; return nil }
    if let a=scrollBefore, let b=bars.first.flatMap({ attr($0,kAXValueAttribute) as? NSNumber })?.doubleValue, abs(a-b)>0.00001 { selectionMoved = true }
    if !nameMatches(selectedConversation()) || !sameRow(row, expected) { selectionMoved = true }
    if labels != nil { copySuccesses += 1 }
    return labels
}
// 仅可选地保存当前可见字形；无名称时不据截图猜标签。
func visibleAttachment(_ node: Node, offset: Int) -> String? {
    guard config.captureInlineImages, #available(macOS 14.0, *), CGPreflightScreenCaptureAccess(),
          NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else { return nil }
    var range = CFRange(location: offset, length: 1), value: CFTypeRef?, rect = CGRect.zero
    if let parameter = AXValueCreate(.cfRange, &range),
       AXUIElementCopyParameterizedAttributeValue(node.element, kAXBoundsForRangeParameterizedAttribute as CFString, parameter, &value) == .success,
       let value { _ = AXValueGetValue(value as! AXValue, .cgRect, &rect) }
    if (rect.width < 2 || rect.height < 2), node.value == "\u{fffc}", let f = frame(node.element) { rect = f }
    guard rect.width > 2, rect.height > 2, rect.width <= 128, rect.height <= 128,
          let viewport = frame(history.1), viewport.contains(rect) else { return nil }
    let done = DispatchSemaphore(value: 0)
    var saved: String?
    SCShareableContent.getWithCompletionHandler { content, _ in
        guard let display = content?.displays.first(where: { $0.frame.contains(rect) }) else { done.signal(); return }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let conf = SCStreamConfiguration()
        conf.width = Int(rect.width * 2); conf.height = Int(rect.height * 2)
        conf.sourceRect = CGRect(x: rect.minX - display.frame.minX, y: rect.minY - display.frame.minY, width: rect.width, height: rect.height)
        conf.scalesToFit = true
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: conf) { image, _ in
            defer { done.signal() }
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier,
                  let image, let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { return }
            let name = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined() + ".png"
            let prefix = URL(fileURLWithPath: config.prefix), folder = prefix.lastPathComponent + "-assets"
            let directory = prefix.deletingLastPathComponent().appendingPathComponent(folder)
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                try png.write(to: directory.appendingPathComponent(name), options: .atomic)
                saved = folder + "/" + name
            } catch { }
        }
    }
    // 等待截图回调后才滚动，避免把下一屏图像关联到旧消息。
    guard done.wait(timeout: .now() + 5) == .success else {
        fputs("内嵌图像截图超时，停止以避免错配下一屏\n", stderr); exit(71)
    }
    return saved
}
var snapshotNodes: [[Node]] = [], snapshotRows: [AXUIElement] = []
func snapshot() -> [[String: Any]] {
    var rows: [[String: Any]] = []
    snapshotNodes = []; snapshotRows = []
    // 客户端会把已加载的旧行持续留在 AXTable。只检查最旧端固定数量的行，
    // 保留跨屏重叠；避免为每屏再逐条读取全部历史行的 AX 属性。
    for (index, row) in children(history.0).prefix(80).enumerated() {
        let nodes = walk(row)
        guard nodes.first?.role == "AXRow" else { continue }
        let sender = nodes.first { $0.role == "AXButton" && !($0.title ?? "").isEmpty && $0.title != "按钮" }?.title
        let time = nodes.filter { $0.role == "AXStaticText" }.compactMap { $0.value.flatMap(timestamp) }.first
        let texts = nodes.filter { $0.role == "AXTextArea" && !($0.value ?? "").isEmpty }
        snapshotNodes.append(texts)
        snapshotRows.append(row)
        var rawParts: [String] = [], renderedParts: [String] = [], attachments: [[String: Any]] = []
        var offset = 0
        for node in texts {
            let raw = node.value ?? "", (rendered, parts) = renderAttachments(raw, labels: attachmentLabels(node))
            rawParts.append(raw); renderedParts.append(rendered)
            for var part in parts {
                let localOffset = part["offset_utf16"] as? Int ?? 0
                if let path = visibleAttachment(node, offset: localOffset) {
                    part["image_path"] = path; part["image_source"] = "visible_screen_crop"; part["visual_only"] = true
                }
                part["offset_utf16"] = offset + localOffset; attachments.append(part)
            }
            offset += (raw as NSString).length + 5
        }
        var bodyRaw = rawParts.joined(separator: "\n---\n"), body = renderedParts.joined(separator: "\n---\n")
        if body.isEmpty {
            let metadata = nodes.filter { $0.role == "AXStaticText" }.compactMap(\.value).compactMap { value -> String? in
                var text = value
                if let range = text.range(of: timePattern, options: .regularExpression) { text.removeSubrange(range) }
                text = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if text.isEmpty || text == sender || text.range(of: #"^(?:(?:\d{4}|\d{2})/)?\d{1,2}/\d{1,2}$"#, options: .regularExpression) != nil { return nil }
                return text
            }.joined(separator: "\n")
            bodyRaw = metadata; body = metadata
        }
        let nonText = body.isEmpty
        if nonText { body = "[非文本消息：内容未暴露]" }
        var missing: [String] = []
        if sender == nil { missing.append("sender") }
        if time == nil { missing.append("time_text") }
        let outgoing: Bool? = sender.flatMap {
            if $0 == "我" { return true }
            if config.selfNames.isEmpty { return nil }
            return selfNames.contains($0) || selfNames.contains(person($0))
        }
        let contentIncomplete = nonText || attachments.contains { ($0["identified"] as? Bool) != true }
        let type = nonText ? "image_or_media" : (texts.count > 1 ? "quote" : (attachments.isEmpty ? "text" : "text_with_attachments"))
        rows.append(["sender": sender as Any? ?? NSNull(), "time_text": time as Any? ?? NSNull(),
                     "body": body, "body_raw": bodyRaw, "message_type": type,
                     "links": nodes.filter { $0.role == "AXLink" }.compactMap(\.title),
                     "attachments": attachments, "content_incomplete": contentIncomplete,
                     "unresolved_fields": missing, "row_path": "history.\(index)", "context_only": false,
                     "is_outgoing": outgoing as Any? ?? NSNull(), "direction_evidence": "explicit_history_sender"])
    }
    return rows
}
func enrichInlineText(_ rows: inout [[String: Any]], newCount: Int) {
    guard config.copyInlineText else { return }
    var counts: [String:Int] = [:]
    for row in rows { counts[rowKey(row),default:0] += 1 }
    for i in 0..<min(newCount, rows.count) {
        guard i < snapshotNodes.count && i < snapshotRows.count else { break }
        let nodes = snapshotNodes[i]
        var attachments = rows[i]["attachments"] as? [[String: Any]] ?? []
        if attachments.isEmpty { continue }
        if counts[rowKey(rows[i]),default:0] > 1 { ambiguousSkipped += 1; continue }
        var offset = 0, labels: [Int: String] = [:]
        for node in nodes {
            let raw = node.value ?? ""
            if raw.contains("\u{fffc}"), let resolved = inlineTextFromClipboard(node, row: snapshotRows[i], expected: rows[i]) {
                for (local, label) in resolved { labels[offset + local] = label }
            }
            offset += (raw as NSString).length + 5
            if clipboardDisabled || selectionMoved { break }
        }
        for index in attachments.indices {
            if let at=attachments[index]["offset_utf16"] as? Int,let label=labels[at] {
                attachments[index]["label"] = label; attachments[index]["identified"] = true
                attachments[index]["label_source"] = "clipboard_text"
            }
        }
        if !labels.isEmpty {
            // 同时保留 AX 原来就暴露的名称。
            for part in attachments where (part["identified"] as? Bool) == true {
                if let at=part["offset_utf16"] as? Int,let label=part["label"] as? String { labels[at] = label }
            }
            rows[i]["body"] = renderAttachments(rows[i]["body_raw"] as? String ?? "", labels: labels).0
            rows[i]["attachments"] = attachments
            rows[i]["content_incomplete"] = attachments.contains { ($0["identified"] as? Bool) != true }
        }
        if clipboardDisabled || selectionMoved { break }
    }
}
var ordered: [[String: Any]] = [], pending: [[String: Any]] = []
var scrolls = 0, idle = 0, gaps = 0, boundary = false, reachedTop = false
var stop = "max-scrolls"
func save(_ reason: String) {
    var occurrences: [String: Int] = [:]
    let records = ordered.map { item -> [String: Any] in
        var row = item; let key = rowKey(row), n = occurrences[key, default: 0]
        occurrences[key] = n + 1; row["id"] = "\(key)#\(n)"; return row
    }
    let unresolved = records.enumerated().filter { !($0.element["unresolved_fields"] as? [String] ?? []).isEmpty }.map { ["record_index": $0.offset, "fields": $0.element["unresolved_fields"] ?? []] as [String: Any] }
    // 只有「有发送人、缺时间、且不是图片/媒体」的行才阻断边界判定（与 chat_validate.blocking_unresolved_count 一致）：
    // 发送人缺失是内容告警；图片行与无发送人无时间的系统提示（撤回、入群、改群名）在面板里本来就不带时间。
    let blockingUnresolved = unresolved.filter { row in
        let fields = (row["fields"] as? [String]) ?? []
        guard fields.contains("time_text"), !fields.contains("sender"),
              let index = row["record_index"] as? Int, index < records.count else { return false }
        return records[index]["message_type"] as? String != "image_or_media"
    }
    let payload: [String: Any] = ["tool": "wework_history_capture", "version": 3,
        "date_parser_version": 2,
        "conversation": config.conversation, "conversation_type": config.type,
        "is_external_group": headers.contains(where: { ($0.value ?? "").contains("外部群") }) ? true : NSNull(),
        "captured_at": ISO8601DateFormatter().string(from: reference), "target_year": Calendar.current.component(.year, from: since),
        "stop_reason": reason, "boundary_verified": boundary, "scroll_count": scrolls,
        "reached_top": reachedTop, "blocking_unresolved": blockingUnresolved.count,
        "checkpoint_at": ISO8601DateFormatter().string(from: Date()),
        "vertical_scroll_value": verticalScrollValue(history.1) as Any? ?? NSNull(),
        "sequence_gaps": gaps, "dialogs_dismissed": dialogsDismissed, "dialog_blocked": dialogBlocked as Any? ?? NSNull(),
        "record_count": records.count, "records": records,
        "unresolved_rows": unresolved, "unmerged_records": pending,
        "content_incomplete_records": records.filter { ($0["content_incomplete"] as? Bool) == true }.count,
        "inline_text_copy": ["enabled": config.copyInlineText, "attempts": copyAttempts, "successes": copySuccesses, "disabled": clipboardDisabled, "disable_reason": clipboardDisableReason as Any? ?? NSNull(), "ambiguous_skipped": ambiguousSkipped],
        "coverage_complete": (boundary || reachedTop) && gaps == 0 && blockingUnresolved.isEmpty]
    do {
        try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]).write(to: URL(fileURLWithPath: config.prefix + "-raw.json"), options: .atomic)
    } catch { fputs("无法写入采集结果\n", stderr); exit(70) }
}
var dialogsDismissed = 0, dialogsSinceScroll = 0, dialogBlocked: String? = nil
/// 读消息途中弹出的过期提示等：能处理就处理，然后核对会话和面板仍在、重读本屏；处理不了或同一屏反复弹出，停止本会话。
func settleDialogs() -> Bool {
    switch handleDialogs() {
    case .none: return true
    case .handled(let n):
        dialogsDismissed += n; dialogsSinceScroll += n
        if dialogsSinceScroll > 3 { dialogBlocked = "同一屏弹窗点掉后反复出现"; return false }
        usleep(300_000)
        return true
    case .blocked(let why):
        dialogBlocked = why; return false
    }
}
while true {
    guard settleDialogs() else { stop = "blocked-by-dialog"; break }
    guard nameMatches(selectedConversation()) else { stop = "conversation-changed"; break }
    guard frame(history.0) != nil else { stop = "table-lost"; break }
    var current = snapshot(), merged = mergeSnapshot(current, ordered)
    if merged == nil {
        // 延迟渲染先原地复读；未对齐时不把整屏并入旧记录。
        usleep(350_000); current = snapshot(); merged = mergeSnapshot(current, ordered)
    }
    // 面板懒加载时偶发瞬时错位：原地复读最多 3 次再判失败。
    // 原来一次对齐失败就 break，会让整段历史只采到开头几屏（实测 9 次滚动即中止）。
    var gapRetry = 0
    while merged == nil && gapRetry < 3 {
        usleep(600_000)
        current = snapshot(); merged = mergeSnapshot(current, ordered); gapRetry += 1
    }
    guard let initialMerged = merged else { gaps += 1; pending = current; stop = "alignment-gap"; break }
    let overlap = current.count + ordered.count - initialMerged.count
    enrichInlineText(&current, newCount: max(0,current.count - overlap))
    if selectionMoved { gaps += 1; pending = current; stop = "alignment-gap"; break }
    guard let merged = mergeSnapshot(current,ordered) else { gaps += 1; pending = current; stop = "alignment-gap"; break }
    idle = merged.count == ordered.count ? idle + 1 : 0
    ordered = merged
    print("记录面板滚动 \(scrolls)，本屏 \(current.count) 行，累计 \(ordered.count) 条")
    // 记录面板顶部即会话历史起点：滚到顶就不再有更早的消息，覆盖到此为止。
    // 面板默认停在最新端，所以「一屏就装下整段历史」时这里同样成立。
    // 只有「滚动条在顶」且「这一屏没有新增行」才算读到会话开头。
    // 单看滚动值为 0 会误判：面板尚未加载完、滚轮空转、或滚动条刚回弹时都是 0，
    // 实测会把只读到 2025-01 的会话记成「已到顶」，从而把错误的最早时间当成会话起点。
    let scrollValue = verticalScrollValue(history.1)
    let barAtTop = (scrollValue ?? 1) <= 0.001
    // 内容不足一屏时滚动范围为空，滚动条恒停在最新端（实测 1~4 条的小会话始终为 1.0），
    // 向上滚既无新增也不改变滚动条，于是永远等不到「归零」，只能以 no-new-content 收场，
    // 结果是 reached_top 恒为 false、状态恒为 partial 并每轮重采。
    // 滚满 idleLimit 次仍无新增且滚动条始终在最新端 → 一屏就是全部。
    // 该依据弱于「滚动条归零」（无法完全排除渲染未完成），故不直接认到顶，
    // 单独标记 stop_reason 交由上层二次确认。
    let screenNotFull = (scrollValue ?? 0) >= 0.999 && idle >= config.idleLimit
    if (barAtTop && idle >= 1) || screenNotFull {
        // 面板滚到顶会触发更早消息的懒加载，但滚动条先归零、内容后到。
        // 直接判到顶会漏掉顶部若干屏（实测漏掉 4 天）。这里补一次滚动并复读，
        // 只有当这次仍无新增内容时才认可到顶。
        // 方向必须与主循环一致：正值 = 向更早的消息滚动（主循环正是这样回溯到 2025 的）。
        if let area = frame(history.1) {
            let pt = CGPoint(x: area.midX, y: area.midY)
            CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
            let ev = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: config.delta, wheel2: 0, wheel3: 0)
            ev?.location = pt; ev?.post(tap: .cghidEventTap)
        }
        usleep(900_000)
        let again = snapshot()
        if let m2 = mergeSnapshot(again, ordered), m2.count > ordered.count {
            ordered = m2
        } else if screenNotFull {
            stop = "top-no-scrollbar"; break
        } else {
            reachedTop = true; stop = "reached-top"; break
        }
    }
    if let earliest = current.compactMap({ recordDate($0, relativeTo: reference) }).min(), earliest < since {
        boundary = true; stop = "reached-since"; break
    }
    if config.noScroll { stop = "no-scroll-test"; break }
    if idle >= config.idleLimit { stop = "no-new-content"; break }
    if scrolls >= config.maxScrolls { break }
    guard let area = frame(history.1) else { stop = "scroll-position-unavailable"; break }
    let point = CGPoint(x: area.midX, y: area.midY)
    guard onAwakeDisplay(point) else { stop = "scroll-target-offscreen"; break }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: config.delta, wheel2: 0, wheel3: 0)
    event?.location = point; event?.post(tap: .cghidEventTap)
    scrolls += 1
    dialogsSinceScroll = 0
    if scrolls % config.checkpointEvery == 0 { save("in-progress") }
    usleep(useconds_t(config.settleMs * 1000))
}
save(stop)
print("记录面板采集结束：\(ordered.count) 条，停止 \(stop)，序列缺口 \(gaps)，已处理弹窗 \(dialogsDismissed) 个")
if let why = dialogBlocked { fputs("弹窗阻塞本会话：\(why)\n", stderr); exit(25) }
