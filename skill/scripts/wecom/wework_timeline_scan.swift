//  左侧会话列表「最后聊天时间」时间线扫描器
//  用途：把左侧会话列表从顶部向下逐屏滚动，读取每一行的「最后聊天时间」，
//        产出按侧栏顺序排列的时间线清单，并验证实际列表终点。
//
//  关键事实（实测，勿凭直觉改）：
//  1. 置顶或分区可能破坏日期顺序；必须扫描到已验证终点，旧年份不作为终点；
//  2. 未渲染的行拿不到时间文本；而且刚滚入视口的行，时间标签会**延迟一帧**渲染，
//     所以必须「小步长 + 重叠采样」，否则整段 rows 的 time 全为 null（实测出现过 480 行全空）；
//     且**每次滚动的位移必须小于一屏行数**：视口只有 14 行，滚 15 行时就必然漏掉每屏 1 行
//     （实测漏掉 163/2458，缺口是 14,29,44,… 每 15 个一个，非常规律）。默认 -26 行 ≈ 12 行位移。
//  3. 表格支持 AXVisibleRows，只读可视行即可，避免每次遍历全部 AXRow；
//  4. 以导航表、搜索框和右侧分隔条共同识别会话侧栏，不按行数或宽度猜表；
//  5. 滚动锚点必须落在窗口内（表格 frame 高度是内容总高度，中心点在屏幕外）。
//
//  用法：
//    swiftc -O wework_timeline_scan.swift -o /tmp/tl
//    /tmp/tl --output timeline.json --stop-year 2025

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
func frame(_ e: AXUIElement) -> CGRect? {
    guard let p = attr(e, kAXPositionAttribute), let s = attr(e, kAXSizeAttribute) else { return nil }
    var pt = CGPoint.zero, sz = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &pt), AXValueGetValue(s as! AXValue, .cgSize, &sz) else { return nil }
    return CGRect(origin: pt, size: sz)
}
func onAwakeDisplay(_ point: CGPoint) -> Bool {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return false }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return false }
    return ids.prefix(Int(count)).contains { CGDisplayIsAsleep($0) == 0 && CGDisplayBounds($0).contains(point) }
}

func frameOf(_ e: AXUIElement) -> CGRect? { frame(e) }
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

// 时间文本的五种形态：YYYY/M/D、M月D日、HH:MM、星期X/周X、昨天/前天/今天
// 五种形态，缺一不可：
//   2025/12/25   跨年（去年及更早）
//   1/4          今年内（**没有「月」「日」字样，只有斜杠**，最容易漏）
//   10月23日      部分版本/部分场景
//   10:08        今天
//   星期四        最近 7 天
let absoluteRe = #"^((?:\d{4}|\d{2}))/(\d{1,2})/(\d{1,2})$"#
let monthDaySlashRe = #"^(\d{1,2})/(\d{1,2})$"#
let monthDayRe = #"^(\d{1,2})月(\d{1,2})日$"#
let clockRe = #"^\d{1,2}:\d{2}$"#
let weekRe = #"^(星期|周)[一二三四五六日天]$"#
func isTime(_ s: String) -> Bool {
    if s == "今天" || s == "昨天" || s == "前天" { return true }
    return [absoluteRe, monthDaySlashRe, monthDayRe, clockRe, weekRe].contains { s.range(of: $0, options: .regularExpression) != nil }
}
let currentYear = Calendar.current.component(.year, from: Date())
func timeYear(_ s: String?) -> Int? {
    guard let s else { return nil }
    if s.range(of: absoluteRe, options: .regularExpression) != nil {
        let first = String(s.split(separator: "/").first ?? "")
        return first.count == 2 ? (Int(first).map { 2000 + $0 }) : Int(first)
    }
    // 1/4 这种只出现在「今年」，直接按当前年份归类；但若月份晚于今天，说明是去年
    if s.range(of: monthDaySlashRe, options: .regularExpression) != nil || s.range(of: monthDayRe, options: .regularExpression) != nil {
        let digits = s.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
        if digits.count >= 2 {
            let (mo, d) = (digits[0], digits[1])
            let now = Date()
            let cal = Calendar.current
            let todayM = cal.component(.month, from: now)
            let todayD = cal.component(.day, from: now)
            if mo > todayM || (mo == todayM && d > todayD) { return currentYear - 1 }
        }
        return currentYear
    }
    return nil
}

var options = (output: "timeline.json", stopYear: 2025, maxScrolls: 400, settleMs: 900, delta: Int32(-26), startAtTop: true)
var args = Array(CommandLine.arguments.dropFirst())
var i = 0
while i + 1 < args.count {
    let k = args[i], v = args[i + 1]
    switch k {
    case "--output": options.output = v
    case "--stop-year": options.stopYear = Int(v) ?? options.stopYear
    case "--max-scrolls": options.maxScrolls = Int(v) ?? options.maxScrolls
    case "--settle-ms": options.settleMs = Int(v) ?? options.settleMs
    case "--scroll-delta": options.delta = Int32(v) ?? options.delta
    case "--no-top": options.startAtTop = false
    default: break
    }
    i += 2
}

guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): false] as CFDictionary) else {
    fputs("缺少辅助功能权限。\n", stderr); exit(2)
}
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.WeWorkMac").first else {
    fputs("企业微信未运行。\n", stderr); exit(3)
}
let ae = AXUIElementCreateApplication(app.processIdentifier)
app.activate(options: [.activateAllWindows])
usleep(400000)
var focused: CFTypeRef?
let focusedOK = AXUIElementCopyAttributeValue(ae, kAXFocusedWindowAttribute as CFString, &focused) == .success && focused != nil
var winCandidates: [AXUIElement] = []
if focusedOK { winCandidates.append(focused as! AXUIElement) }
var winsRef: CFTypeRef?
if AXUIElementCopyAttributeValue(ae, kAXWindowsAttribute as CFString, &winsRef) == .success,
   let wins = winsRef as? [AXUIElement] { winCandidates.append(contentsOf: wins) }
guard !winCandidates.isEmpty else { exit(4) }

guard let pick = uniqueSidebar(winCandidates) else { fputs("未找到唯一的主窗口会话侧栏\n", stderr); exit(6) }
let root = pick.window
let table = pick.table
guard let wf = frame(root), let viewport = frame(pick.scroll) else { exit(5) }
let initialDisplays = displayLayout()
func lastIndexedRow(_ table: AXUIElement) -> Int {
    // The live sidebar has a zero-index header: AXRow count is one larger than the last chat index.
    children(table).filter { role($0) == "AXRow" }
        .compactMap { (attr($0, "AXIndex") as? NSNumber)?.intValue }.filter { $0 > 0 }.max() ?? 0
}
var allRows = lastIndexedRow(table)
print("会话表：宽度 \(Int(viewport.width))，AXRow 总数 \(allRows)")

func visibleRows() -> [AXUIElement] {
    if let rows = attr(table, "AXVisibleRows") as? [AXUIElement], !rows.isEmpty { return rows }
    return children(table).filter { role($0) == "AXRow" }
}

// 行的 AXIndex 是它在整张表里的绝对序号（从 1 开始），比「首次出现顺序」可靠得多：
// 不会因为重叠采样而错位，也能直接看出是否有整段被跳过。

var slot: [Int: (name: String, time: String?)] = [:]
var pending: [String] = []      // 拿不到 AXIndex 的兜底

// 侧栏按「置顶 + 最新消息时间从新到旧」排列：任一在 [start, today] 内有消息的会话，
// 其 last_activity 必然 >= start。因此扫到连续 N 行都明确早于 start 的年份时就无需再往下扫。
// 不能单行判断：时间标签延迟一帧渲染、有空时间行、置顶区可能破坏顺序。
// 空时间/无年份的行打断连续；前 30 行不参与判断，避开置顶区。
func crossedStartBoundary(slot: [Int: (name: String, time: String?)],
                          stopYear: Int, minRun: Int = 20, skipHead: Int = 30) -> Int? {
    let keys = slot.keys.sorted()
    guard stopYear > 0, keys.count > skipHead + minRun else { return nil }
    var run = 0, startOrder = 0
    for k in keys where k > skipHead {
        guard let t = slot[k]?.time, let y = timeYear(t) else {
            run = 0; startOrder = 0; continue
        }
        if y < stopYear {
            if run == 0 { startOrder = k }
            run += 1
            if run >= minRun { return startOrder }
        } else {
            run = 0; startOrder = 0
        }
    }
    return nil
}
// 机器人徽章：实测在会话名右侧是 AXTextArea，值形如 "BOT\u{200B}"（带零宽字符），
// 不是 AXStaticText，所以按字号收集会话名时完全看不到。单独记一份供上层自动排除。
var botNames: Set<String> = []

func saveTimeline(stopReason: String, crossedAt: Int? = nil) throws {
    let keys = slot.keys.sorted()
    let entries: [[String: Any]] = keys.map { idx in
        let v = slot[idx]!
        var e: [String: Any] = ["name": v.name, "order": idx]
        e["last_activity"] = v.time as Any
        if let y = timeYear(v.time) { e["year"] = y }
        return e
    }
    if !pending.isEmpty {
        print("注意：\(pending.count) 行拿不到 AXIndex，未纳入（前 3：\(pending.prefix(3))）")
    }
    let payload: [String: Any] = [
        "tool": "wework_timeline_scan",
        "version": 3,
        "captured_at": ISO8601DateFormatter().string(from: Date()),
        "total_rows": allRows,
        "conversation_count": slot.count,
        "with_last_activity": slot.values.filter { $0.time != nil }.count,
        "unindexed_rows": pending.count,
        "first_index": keys.first ?? 0,
        "last_index": keys.last ?? 0,
        "crossed_at": crossedAt as Any,
        "stop_reason": stopReason,
        "ordering": "sidebar_order",
        "end_verified": stopReason == "verified-list-end",
        "coverage_note": stopReason.hasPrefix("crossed-start-")
            ? "边界扫描：从顶部连续覆盖到窗口起点年份边界；边界之后的会话最后活跃早于窗口起点，不可能有窗口内消息。"
            : "全列表扫描；仅实际终点与连续索引证明覆盖。旧年份、滚动停滞和布局变化均不能证明完整。",
        "conversations": entries,
        "bots": Array(botNames).sorted(),
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
    try data.write(to: URL(fileURLWithPath: options.output), options: .atomic)
}

func requireStableLayout() {
    guard layoutMatches(wf, viewport, initialDisplays, frame(root), frame(pick.scroll), displayLayout()) else {
        do { try saveTimeline(stopReason: "interrupted-layout-change") }
        catch { fputs("布局变化时保存扫描检查点失败：\(error)\n", stderr) }
        fputs("窗口或显示器布局已变化，已停止扫描；重新运行以刷新候选与坐标\n", stderr)
        exit(22)
    }
}

@discardableResult
func harvest() -> Int {
    requireStableLayout()
    var changed = 0
    var updated = slot
    var nextPending = pending
    for row in visibleRows() {
        var texts: [(CGFloat, CGFloat, String)] = []
        var botBadge = false
        func walk(_ n: AXUIElement, _ d: Int) {
            if d > 6 { return }
            if role(n) == "AXStaticText", let v = string(n, kAXValueAttribute), !v.isEmpty, let f = frame(n) {
                texts.append((f.minY, f.minX, v))
            }
            if role(n) == "AXTextArea", let v = string(n, kAXValueAttribute) {
                let cleaned = v.replacingOccurrences(of: "\u{200B}", with: "")
                    .replacingOccurrences(of: "\u{FEFF}", with: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if cleaned.uppercased() == "BOT" { botBadge = true }
            }
            for c in children(n) { walk(c, d + 1) }
        }
        walk(row, 0)
        guard !texts.isEmpty else { continue }
        texts.sort { $0.0 == $1.0 ? $0.1 < $1.1 : $0.0 < $1.0 }
        let midX = viewport.midX
        let timeHere = texts.first(where: { isTime($0.2) && $0.1 > midX })?.2
        guard let name = texts.first(where: { !($0.1 > midX && isTime($0.2)) && !$0.2.hasPrefix("@") && $0.2.count <= 120 })?.2 else { continue }
        if botBadge { botNames.insert(name) }
        let t = timeHere
        if let idx = (attr(row, "AXIndex") as? NSNumber)?.intValue, idx > 0 {
            if let old = updated[idx] {
                if old.name != name {
                    try? saveTimeline(stopReason: "interrupted-list-change")
                    fputs("扫描期间列表序号对应会话发生变化，保存片段并停止\n", stderr)
                    exit(22)
                }
                if old.time == nil, let t { updated[idx] = (old.name, t); changed += 1 }
            } else {
                updated[idx] = (name, t)
                if t != nil { changed += 1 }
            }
        } else if !nextPending.contains(name) {
            nextPending.append(name)
        }
    }
    requireStableLayout()
    slot = updated
    pending = nextPending
    return changed
}

func firstVisibleIndex() -> Int? {
    for r in visibleRows() {
        if let i = (attr(r, "AXIndex") as? NSNumber)?.intValue, i > 0 { return i }
    }
    return nil
}
func lastVisibleIndex() -> Int? {
    for r in visibleRows().reversed() {
        if let i = (attr(r, "AXIndex") as? NSNumber)?.intValue, i > 0 { return i }
    }
    return nil
}

guard frame(table) != nil else { exit(7) }
// 扫描仅在布局稳定期间继续；变化时保存已核验的片段并停止，续跑重新发现候选。
func currentAnchor() -> CGPoint? {
    requireStableLayout()
    guard let tf = frame(table), let wfr = frame(root) else { return nil }
    let p = CGPoint(x: tf.midX, y: wfr.midY)
    return onAwakeDisplay(p) ? p : nil
}
guard let initialAnchor = currentAnchor() else {
    fputs("会话列表滚动位置已移出所有唤醒的活动显示器；请把企业微信窗口移回屏幕\n", stderr)
    exit(22)
}
print(String(format: "滚动锚点 (%.0f, %.0f)", initialAnchor.x, initialAnchor.y))

func wheel(_ delta: Int32, settle: Int) {
    guard let anchor = currentAnchor() else {
        fputs("扫描过程中窗口离开了可见显示器（例如拔掉扩展屏后企业微信跳回主屏），本轮停止；直接重新运行即可继续\n", stderr)
        exit(22)
    }
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: anchor, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(15000)
    let ev = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: delta, wheel2: 0, wheel3: 0)
    ev?.location = anchor
    ev?.post(tap: .cghidEventTap)
    usleep(useconds_t(settle * 1000))
}

// 第一步：回到顶部。必须「滚到真的不动为止」，不能用固定次数（本机需 ~300 次）
if options.startAtTop {
    var idle = 0
    var lastIdx = -1
    for round in 1...120 {
        for _ in 0..<4 { wheel(70, settle: 110) }
        usleep(250000)
        let idx = firstVisibleIndex() ?? -1
        if idx == 1 { print("已到顶（首行 AXIndex=1）"); break }
        if idx == lastIdx { idle += 1 } else { idle = 0 }
        lastIdx = idx
        if round % 10 == 0 { print("回顶第 \(round) 轮，首行 AXIndex=\(idx)") }
        if idle >= 3 && idx <= 1 { print("已到顶（第 \(round) 轮）"); break }
        if idle >= 6 { print("回顶停滞于 AXIndex=\(idx)，继续向下扫描"); break }
    }
    usleep(900000)
}

// 第二步：扫描实际终点；无进展只能停止为 partial。
func verticalScrollValue(_ scroll: AXUIElement) -> Double? {
    let bars = children(scroll).filter { role($0) == "AXScrollBar" &&
        string($0, kAXOrientationAttribute) == "AXVerticalOrientation" }
    return bars.first.flatMap { attr($0, kAXValueAttribute) as? NSNumber }?.doubleValue
}
_ = harvest()
var stopReason = "max-scrolls"
var idle = 0, bottomStable = 0
var crossedAtValue: Int? = nil
var previousLast = lastVisibleIndex() ?? -1
for step in 1...max(1, options.maxScrolls) {
    wheel(options.delta, settle: options.settleMs)
    let gained = harvest()
    allRows = lastIndexedRow(table)
    let last = lastVisibleIndex() ?? -1
    let unchanged = gained == 0 && last == previousLast
    idle = unchanged ? idle + 1 : 0
    let atBottom = (verticalScrollValue(pick.scroll) ?? -1) >= 0.999999
    let endIndex = allRows > 0 && last == allRows && slot[allRows] != nil
    bottomStable = atBottom && endIndex && unchanged ? bottomStable + 1 : 0
    previousLast = last
    if step % 5 == 0 {
        print("第 \(step) 屏：已知 \(slot.count) 行，末行 \(last)，AXRow 总数 \(allRows)，停滞 \(idle)")
        try saveTimeline(stopReason: "in-progress")
    }
    // 边界提前停止：--stop-year 由调用方按窗口起点年份传入（此前只解析、从未使用）。
    // 只有连续 minRun 行都明确早于该年份才停，避免单行空时间/置顶破坏顺序造成误停。
    if let boundary = crossedStartBoundary(slot: slot, stopYear: options.stopYear) {
        stopReason = "crossed-start-\(options.stopYear)"
        crossedAtValue = boundary
        print("已越过窗口起点年份边界：连续 20 行早于 \(options.stopYear)，边界 AXIndex=\(boundary)")
        break
    }
    if bottomStable >= 3 { stopReason = "verified-list-end"; break }
    if idle >= 8 { stopReason = "no-progress"; break }
}
try saveTimeline(stopReason: stopReason, crossedAt: crossedAtValue)
print("扫描结束：\(slot.count) 行，停止原因=\(stopReason)")
print("已写入 \(options.output)")
