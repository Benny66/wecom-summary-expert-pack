import Cocoa
import ApplicationServices
import Foundation
// 输出重定向到日志文件时默认全缓冲（退出才落盘），实时控制台与卡住检测就看不到进展；改为逐行刷新。
_ = setvbuf(stdout, nil, _IOLBF, 0)

func activeDisplays() -> [(CGDirectDisplayID, CGRect)] {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return [] }
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else { return [] }
    return ids.prefix(Int(count)).map { ($0, CGDisplayBounds($0)) }
}
func pointOnDisplay(_ point: CGPoint, _ displays: [(CGDirectDisplayID, CGRect)]) -> Bool {
    displays.contains { $0.1.contains(point) }
}
func frame(_ element: AXUIElement) -> CGRect? {
    var position: CFTypeRef?, size: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &position) == .success,
          AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &size) == .success,
          let position, let size else { return nil }
    var point = CGPoint.zero, dimensions = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}
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

func fail(_ reason: String, _ code: Int32) -> Never {
    fputs("采集前检查失败：\(reason)\n", stderr)
    exit(code)
}

if CommandLine.arguments.contains("--self-test") {
    let screens: [(CGDirectDisplayID, CGRect)] = [
        (1, CGRect(x: 0, y: 0, width: 1728, height: 1117)),
        (3, CGRect(x: -1920, y: 0, width: 1920, height: 1080))
    ]
    guard pointOnDisplay(CGPoint(x: -1000, y: 500), screens),
          pointOnDisplay(CGPoint(x: 500, y: 500), screens),
          !pointOnDisplay(CGPoint(x: -2100, y: 500), screens) else { exit(70) }
    print("PREFLIGHT_SELF_TEST_OK")
    exit(0)
}

guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): false] as CFDictionary) else {
    fail("当前运行环境缺少辅助功能权限", 2)
}
let session = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
guard (session["CGSSessionScreenIsLocked"] as? Int ?? 0) == 0 else { fail("屏幕已锁定", 20) }
let displays = activeDisplays().filter { CGDisplayIsAsleep($0.0) == 0 }
guard !displays.isEmpty else { fail("没有唤醒的活动显示器", 21) }
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.WeWorkMac").first else {
    fail("企业微信未运行", 3)
}
let application = AXUIElementCreateApplication(app.processIdentifier)
var focused: CFTypeRef?
let focusedResult = AXUIElementCopyAttributeValue(application, kAXFocusedWindowAttribute as CFString, &focused)
var allWindows: [AXUIElement] = []
if focusedResult == .success, let focused { allWindows.append(focused as! AXUIElement) }
var windowsRef: CFTypeRef?
if AXUIElementCopyAttributeValue(application, kAXWindowsAttribute as CFString, &windowsRef) == .success,
   let wins = windowsRef as? [AXUIElement] { allWindows.append(contentsOf: wins) }
guard !allWindows.isEmpty else { fail("没有企业微信窗口", 4) }
guard let pick = uniqueSidebar(allWindows), let bounds = frame(pick.window),
      let viewport = frame(pick.scroll) else { fail("未找到唯一的主窗口会话侧栏", 4) }
let sidebar = CGPoint(x: viewport.midX, y: viewport.midY)
let content = CGPoint(x: (viewport.maxX + bounds.maxX) * 0.5, y: bounds.midY)
guard pointOnDisplay(sidebar, displays), pointOnDisplay(content, displays) else {
    fail("企业微信主窗口的侧栏或消息区已移出所有唤醒的活动显示器；请把窗口移回屏幕", 22)
}
let hitIDs = displays.filter { !$0.1.intersection(bounds).isEmpty }.map { Int($0.0) }
let loadedRows = children(pick.table).filter { string($0, kAXRoleAttribute) == "AXRow" }
let rowIndexes = loadedRows.compactMap { (attr($0, "AXIndex") as? NSNumber)?.intValue }.filter { $0 > 0 }
let result: [String: Any] = [
    "ready": true,
    "sidebar_frame": [viewport.minX, viewport.minY, viewport.width, viewport.height],
    "sidebar_loaded_rows": loadedRows.count,
    "sidebar_index_min": rowIndexes.min() as Any? ?? NSNull(),
    "sidebar_index_max": rowIndexes.max() as Any? ?? NSNull(),
    "display_count": displays.count,
    "window_display_ids": hitIDs,
    "window_on_external_display": hitIDs.contains { $0 != Int(CGMainDisplayID()) },
    "window_frame": [bounds.minX, bounds.minY, bounds.width, bounds.height],
    "coordinate_system": "Quartz/AX global display coordinates"
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
