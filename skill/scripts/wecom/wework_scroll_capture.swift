import Cocoa
import ApplicationServices
import ScreenCaptureKit
import CryptoKit
import Foundation
// 输出重定向到日志文件时默认全缓冲（退出才落盘），实时控制台与卡住检测就看不到进展；改为逐行刷新。
_ = setvbuf(stdout, nil, _IOLBF, 0)

// 企业微信当前会话向上滚动只读采集器。
// 仅读取 Accessibility 属性并发送滚动事件；不读数据库、不点击、不输入、不发送消息。

struct Config {
    var bundleID = "com.tencent.WeWorkMac"
    var year = Calendar.current.component(.year, from: Date())
    var outputPrefix = "wework-capture"
    var maxScrolls = 500
    var idleLimit = 6
    /// 完整目标起始日。不能只保存月日，否则跨年区间会在第一次年份回绕时提前停止。
    var sinceDate: Date?
    var expectedConversationType: String?
    var checkpointEvery = 10
    var settleMilliseconds = 800
    var scrollDelta: Int32 = 20
    var noScroll = false
    var prompt = false
    var selfTestDates = false
    /// 用屏幕像素做方向取证（需要「屏幕录制」权限）。关闭时只用 AX 几何位置。
    var pixelDirection = false
}

struct RawRecord: Codable {
    let id: String
    let sender: String?
    let timeText: String?
    let body: String
    let messageType: String
    let links: [String]
    let rowPath: String
    let contextOnly: Bool
    let isOutgoing: Bool?
    /// 方向判定依据：geometry（头像坐标）或 screencapture（像素取证）。
    let directionEvidence: String?

    enum CodingKeys: String, CodingKey {
        case id, sender, body, links
        case timeText = "time_text"
        case messageType = "message_type"
        case rowPath = "row_path"
        case contextOnly = "context_only"
        case isOutgoing = "is_outgoing"
        case directionEvidence = "direction_evidence"
    }
}

struct CaptureFile: Codable {
    let tool: String
    let version: Int
    var dateParserVersion: Int = 2
    let capturedAt: String
    let targetYear: Int
    let stopReason: String
    /// 边界是否被验证：只有真正滚过了目标起始日（reached-since）或跨到上一年
    /// （reached-previous-year）才算数。「滚不动了」既可能是到顶，也可能是客户端
    /// 分页暂时不给数据，不能当成采全——这一点必须显式区分，不能靠猜。
    let boundaryVerified: Bool
    let scrollCount: Int
    let recordCount: Int
    let sequenceGaps: Int
    let memberCount: Int?
    let records: [RawRecord]

    enum CodingKeys: String, CodingKey {
        case tool, version, records
        case dateParserVersion = "date_parser_version"
        case capturedAt = "captured_at"
        case targetYear = "target_year"
        case stopReason = "stop_reason"
        case boundaryVerified = "boundary_verified"
        case scrollCount = "scroll_count"
        case recordCount = "record_count"
        case sequenceGaps = "sequence_gaps"
        case memberCount = "member_count"
    }
}

func help() {
    print("""
    企业微信当前会话向上滚动只读采集器

    用法：swift wework_scroll_capture.swift [选项]

    --year <YYYY>          目标起始年份，默认当前年份；只在早于该年份时停止
    --output-prefix <前缀> 原始输出文件前缀，默认 wework-capture
    --max-scrolls <N>      最大滚动次数，默认 500
    --idle-limit <N>       顶部无变化多少次后停止，默认 6
    --checkpoint-every <N> 每多少次滚动写入一次检查点，默认 10
    --settle-ms <N>        每次滚动后的等待毫秒数，默认 800
    --scroll-delta <N>     滚动量，默认 20（向上）；方向相反时可试 -20
    --no-scroll            只采集当前已加载内容，用于首次测试
    --prompt               未授权时弹出辅助功能授权提示
    --pixel-direction      用屏幕像素取证判定消息方向（需屏幕录制权限；取不到则回退几何判定）
    --conversation-type <single|group|unknown>  已知会话类型；unknown 时不强猜
    --self-test-dates      只验证跨年日期逻辑，不访问企业微信
    --help                 显示帮助
    """)
}

func parseConfig() -> Config? {
    var c = Config()
    var args = Array(CommandLine.arguments.dropFirst())
    while !args.isEmpty {
        let arg = args.removeFirst()
        switch arg {
        case "--help", "-h": help(); return nil
        case "--no-scroll": c.noScroll = true
        case "--prompt": c.prompt = true
        case "--pixel-direction": c.pixelDirection = true
        case "--self-test-dates": c.selfTestDates = true
        case "--year", "--output-prefix", "--max-scrolls", "--idle-limit", "--checkpoint-every", "--settle-ms", "--scroll-delta", "--since", "--conversation-type":
            guard !args.isEmpty else { fputs("缺少参数：\(arg)\n", stderr); exit(64) }
            let value = args.removeFirst()
            switch arg {
            case "--output-prefix": c.outputPrefix = value
            case "--since":
                let formatter = DateFormatter()
                formatter.calendar = Calendar(identifier: .gregorian)
                formatter.locale = Locale(identifier: "en_US_POSIX")
                formatter.timeZone = .current
                formatter.dateFormat = "yyyy-MM-dd"
                guard let parsed = formatter.date(from: value) else {
                    fputs("--since 需要 YYYY-MM-DD 格式，收到：\(value)\n", stderr); exit(64)
                }
                c.sinceDate = Calendar.current.startOfDay(for: parsed)
            case "--conversation-type":
                guard ["single", "group", "unknown"].contains(value) else {
                    fputs("--conversation-type 只能是 single、group 或 unknown。\n", stderr); exit(64)
                }
                c.expectedConversationType = value == "unknown" ? nil : value
            case "--scroll-delta":
                guard let n = Int32(value), abs(n) <= 200 else { fputs("滚动量无效。\n", stderr); exit(64) }
                c.scrollDelta = n
            default:
                guard let n = Int(value), n > 0 else { fputs("数值参数无效：\(arg)\n", stderr); exit(64) }
                switch arg {
                case "--year": c.year = n
                case "--max-scrolls": c.maxScrolls = min(n, 5000)
                case "--idle-limit": c.idleLimit = min(n, 50)
                case "--checkpoint-every": c.checkpointEvery = min(n, 100)
                case "--settle-ms": c.settleMilliseconds = min(n, 10000)
                default: break
                }
            }
        default: fputs("未知参数：\(arg)\n", stderr); exit(64)
        }
    }
    return c
}

func axAttr(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value as AnyObject?
}

func axString(_ element: AXUIElement, _ name: String) -> String? {
    if let value = axAttr(element, name) as? String { return value }
    if let value = axAttr(element, name) as? NSAttributedString { return value.string }
    return nil
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (axAttr(element, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func frameOf(_ element: AXUIElement) -> CGRect? {
    guard let posValue = axAttr(element, kAXPositionAttribute),
          let sizeValue = axAttr(element, kAXSizeAttribute) else { return nil }
    var pos = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(posValue as! AXValue, .cgPoint, &pos),
          AXValueGetValue(sizeValue as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: pos, size: size)
}

func walk(_ element: AXUIElement, path: String = "root") -> [(AXUIElement, String)] {
    var result = [(element, path)]
    for (index, child) in children(element).enumerated() {
        result.append(contentsOf: walk(child, path: "\(path).\(index)"))
    }
    return result
}

/// 一次跨进程调用取回多个属性，替代对同一节点反复调用 axString（每次 1~2 次 IPC）。
struct AXNode {
    let element: AXUIElement
    let role: String
    let value: String?
    let title: String?
    let desc: String?
    let frame: CGRect?
}

private let nodeAttributes: CFArray = [
    kAXRoleAttribute, kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
    kAXPositionAttribute, kAXSizeAttribute,
] as CFArray

func readNode(_ element: AXUIElement) -> AXNode {
    var values: CFArray?
    var role = "", value: String?, title: String?, desc: String?
    var pos: CGPoint?, size: CGSize?
    if AXUIElementCopyMultipleAttributeValues(element, nodeAttributes, AXCopyMultipleAttributeOptions(rawValue: 0), &values) == .success,
       let list = values as? [AnyObject], list.count == 6 {
        func text(_ item: AnyObject) -> String? {
            if let s = item as? String { return s }
            if let a = item as? NSAttributedString { return a.string }
            return nil
        }
        role = text(list[0]) ?? ""
        value = text(list[1]); title = text(list[2]); desc = text(list[3])
        if CFGetTypeID(list[4]) == AXValueGetTypeID() {
            var p = CGPoint.zero
            if AXValueGetValue(list[4] as! AXValue, .cgPoint, &p) { pos = p }
        }
        if CFGetTypeID(list[5]) == AXValueGetTypeID() {
            var s = CGSize.zero
            if AXValueGetValue(list[5] as! AXValue, .cgSize, &s) { size = s }
        }
    }
    let frame: CGRect? = (pos != nil && size != nil) ? CGRect(origin: pos!, size: size!) : nil
    return AXNode(element: element, role: role, value: value, title: title, desc: desc, frame: frame)
}

/// 单次深度遍历：每个节点只做一次批量属性读取。
func walkNodes(_ element: AXUIElement) -> [AXNode] {
    var result = [readNode(element)]
    for child in children(element) { result.append(contentsOf: walkNodes(child)) }
    return result
}

func sha256(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}

let timePattern = #"^(?:(?:(?:(?:19|20)\d{2}[-/年]|\d{2}/)\d{1,2}[-/月]\d{1,2}日?)|(?:\d{1,2}[/月]\d{1,2}日?)|(?:星期[一二三四五六日天])|(?:周[一二三四五六日天])|前天|昨天|今天)(?:\s+\d{1,2}:\d{2})?"#
let yearPattern = #"^(?:(?:19|20)\d{2}(?=[年/\-])|\d{2}(?=/\d{1,2}/\d{1,2}日?(?:\s|$)))"#

func isTimeText(_ text: String) -> Bool {
    text.range(of: timePattern, options: .regularExpression) != nil || text.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil
}

func explicitYear(_ text: String) -> Int? {
    guard let match = text.range(of: yearPattern, options: .regularExpression) else { return nil }
    guard let year = Int(text[match]) else { return nil }
    return year < 100 ? 2000 + year : year
}

func monthDay(_ text: String) -> Int? {
    if explicitYear(text) != nil {
        let numbers = text.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
        guard numbers.count >= 3, (1...12).contains(numbers[1]), (1...31).contains(numbers[2]) else { return nil }
        return numbers[1] * 32 + numbers[2]
    }
    guard let match = text.range(of: #"^(\d{1,2})[/月](\d{1,2})日?(?=$|\s)"#, options: .regularExpression) else { return nil }
    let value = String(text[match])
    let numbers = value.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
    guard numbers.count == 2, (1...12).contains(numbers[0]), (1...31).contains(numbers[1]) else { return nil }
    return numbers[0] * 32 + numbers[1]
}

func strictMessageDate(_ year: Int, _ month: Int, _ day: Int) -> Date? {
    let cal = Calendar(identifier:.gregorian)
    guard let date = cal.date(from:DateComponents(year:year,month:month,day:day)) else { return nil }
    let actual = cal.dateComponents([.year,.month,.day],from:date)
    return actual.year == year && actual.month == month && actual.day == day ? date : nil
}

func messageDate(_ text: String, relativeTo reference: Date) -> Date? {
    let cal = Calendar(identifier: .gregorian)
    let numbers = text.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
    if let year = explicitYear(text), numbers.count >= 3 {
        return strictMessageDate(year,numbers[1],numbers[2])
    }
    if let md = monthDay(text) {
        let month = md / 32, day = md % 32
        let today = cal.dateComponents([.year, .month, .day], from: reference)
        guard var year = today.year,
              let candidate = strictMessageDate(year,month,day) else { return nil }
        if candidate > cal.startOfDay(for: reference) {
            year -= 1
        }
        return strictMessageDate(year,month,day)
    }
    let start = cal.startOfDay(for: reference)
    if text.hasPrefix("今天") { return start }
    if text.hasPrefix("昨天") { return cal.date(byAdding: .day, value: -1, to: start) }
    if text.hasPrefix("前天") { return cal.date(byAdding: .day, value: -2, to: start) }
    return nil
}

/// 屏幕像素取证：单聊不渲染姓名，纯 AX 坐标会偶发返回镜像值（同一静止行能读到 520 与 1526），
/// 因此可选地截取消息区，用「头像所在一侧」做判断——头像是彩色图像，局部像素方差明显高于空白背景。
/// 需要「屏幕录制」权限；取不到图或两侧差异不足时返回 nil，交由几何判定兜底。
struct PixelProbe {
    /// 截取屏幕上一块矩形。CGWindowListCreateImage 在当前 SDK 已停用，改用 ScreenCaptureKit；
    /// 没有「屏幕录制」权限时拿不到其他 App 的像素，返回 nil，由调用方回退几何判定。
    @available(macOS 14.0, *)
    static func capture(rect: CGRect) -> CGImage? {
        let center = CGPoint(x: rect.midX, y: rect.midY)
        var shot: CGImage?
        let contentReady = DispatchSemaphore(value: 0)
        let shotReady = DispatchSemaphore(value: 0)
        SCShareableContent.getWithCompletionHandler { content, _ in
            defer { contentReady.signal() }
            guard let content, let display = content.displays.first(where: { d in
                abs(center.x - d.frame.midX) <= d.frame.width / 2 && abs(center.y - d.frame.midY) <= d.frame.height / 2
            }) else { return }
            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let conf = SCStreamConfiguration()
            conf.width = max(1, Int(rect.width))
            conf.height = max(1, Int(rect.height))
            conf.sourceRect = CGRect(x: rect.minX - display.frame.minX, y: rect.minY - display.frame.minY,
                                     width: rect.width, height: rect.height)
            conf.scalesToFit = false
            SCScreenshotManager.captureImage(contentFilter: filter, configuration: conf) { image, _ in
                shot = image
                shotReady.signal()
            }
        }
        _ = contentReady.wait(timeout: .now() + 5)
        _ = shotReady.wait(timeout: .now() + 5)
        return shot
    }

    let width: Int
    let height: Int
    let bytes: [UInt8]
    let origin: CGPoint

    init?(rect: CGRect) {
        guard #available(macOS 14.0, *) else { return nil }
        guard let image = PixelProbe.capture(rect: rect) else { return nil }
        width = image.width
        height = image.height
        guard width > 0, height > 0 else { return nil }
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: &buffer, width: width, height: height, bitsPerComponent: 8,
                                  bytesPerRow: width * 4, space: colorSpace,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        bytes = buffer
        origin = rect.origin
    }

    /// 以 (cx, cy) 为中心、边长 size 的窗口内亮度方差；越界返回 nil
    func variance(cx: Int, cy: Int, size: Int) -> Double? {
        let half = size / 2
        var values: [Double] = []
        for y in (cy - half)...(cy + half) {
            for x in (cx - half)...(cx + half) {
                guard x >= 0, y >= 0, x < width, y < height else { continue }
                let offset = (y * width + x) * 4
                let r = Double(bytes[offset]), g = Double(bytes[offset + 1]), b = Double(bytes[offset + 2])
                values.append(0.299 * r + 0.587 * g + 0.114 * b)
            }
        }
        guard values.count >= 4 else { return nil }
        let mean = values.reduce(0, +) / Double(values.count)
        return values.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Double(values.count)
    }

    /// 头像在行首或行尾：右侧更「花」说明是自己的消息（自己的头像在右）
    func outgoing(frame: CGRect) -> Bool? {
        let cy = Int(frame.midY - origin.y)
        let leftX = Int(frame.minX + 20 - origin.x)
        let rightX = Int(frame.maxX - 20 - origin.x)
        guard let left = variance(cx: leftX, cy: cy, size: 16), let right = variance(cx: rightX, cy: cy, size: 16) else { return nil }
        if left < 1 && right < 1 { return nil }            // 全平：多半没截到内容（权限不足）
        if right > left * 1.5 { return true }
        if left > right * 1.5 { return false }
        return nil                                          // 差异不够，不猜
    }
}

var pixelProbe: PixelProbe?

func refreshPixelProbe(_ rect: CGRect, enabled: Bool) {
    guard enabled else { return }
    pixelProbe = PixelProbe(rect: rect)
    if pixelProbe == nil {
        fputs("像素取证不可用（缺少屏幕录制权限或无法截取该区域），本次回退几何判定。\n", stderr)
    }
}

func parseRow(_ row: AXUIElement, path: String, occurrence: Int, midline: CGFloat?, isGroup: Bool?) -> RawRecord? {
    // 整行只遍历一次、每个节点只读一次属性；原实现对同一批节点重复读 role 4~8 轮。
    let nodes = walkNodes(row)
    func existingNode(_ element: AXUIElement) -> AXNode {
        nodes.first { CFEqual($0.element, element) } ?? readNode(element)
    }
    let rowChildren = children(row)
    let cellElement = rowChildren.first { existingNode($0).role == "AXCell" } ?? row
    let directNodes = children(cellElement).map(existingNode)

    let senderFromLabel = directNodes.first {
        $0.role == "AXStaticText" && ($0.value.map { $0.hasSuffix(":") || $0.hasSuffix("：") } ?? false)
    }.flatMap { $0.value }.map { String($0.dropLast()) }
    let senderFromButton = directNodes.first {
        $0.role == "AXButton" && ($0.title ?? "") != "" && $0.title != "按钮"
    }.flatMap { $0.title }
    // 单聊界面不渲染发送者名；引用回复块里的「姓名:」会被误当发送者，因此单聊一律不用标签。
    let sender = isGroup == false ? nil : (senderFromButton ?? senderFromLabel)

    // 群聊有姓名可直接判定，无需推断；单聊只能靠方向。
    // 像素取证优先（更稳），取不到再退回头像几何位置。
    var evidence: String? = nil
    var isOutgoing: Bool? = isGroup == true && sender != nil ? nil : directionFromNodes(nodes, midline: midline)
    if isGroup == true && isOutgoing == false { isOutgoing = nil }
    if isGroup != true {
        if let frame = frameOf(row), let verdict = pixelProbe?.outgoing(frame: frame) {
            isOutgoing = verdict
            evidence = "screencapture"
        } else if isOutgoing != nil {
            evidence = "geometry"
        }
    }
    let time = directNodes.first { $0.role == "AXStaticText" && isTimeText($0.value ?? "") }.flatMap { $0.value }
    let bodies = nodes.filter { $0.role == "AXTextArea" }.compactMap { $0.value }.filter { !$0.isEmpty }
    let links = nodes.filter { $0.role == "AXLink" }.compactMap { $0.title }.filter { !$0.isEmpty }
    let staticMetadata = directNodes.filter { node in
        guard node.role == "AXStaticText", let value = node.value, !value.isEmpty else { return false }
        return !isTimeText(value) && value != sender && value != "\(sender ?? ""):"
    }.compactMap { $0.value }
    let imageDescriptions = nodes.filter { $0.role == "AXImage" }.compactMap { $0.desc }
        .filter { !$0.isEmpty && !$0.contains("avatar") && !$0.contains("head") }

    var body = bodies.joined(separator: "\n---\n")
    if body.isEmpty && !staticMetadata.isEmpty { body = staticMetadata.joined(separator: "\n") }
    let type: String
    if body.contains("\n---\n") { type = "quote" }
    else if bodies.count > 1 { type = "reply_or_rich_text" }
    else if !bodies.isEmpty && !links.isEmpty { type = "text_with_links" }
    else if !bodies.isEmpty { type = "text" }
    else if !staticMetadata.isEmpty { type = "file_card_or_system" }
    else if !imageDescriptions.isEmpty { type = "image_or_media" }
    else { type = "system_or_other" }

    let contextOnly = body.isEmpty && sender == nil && imageDescriptions.isEmpty && isOutgoing == nil
    if contextOnly && time == nil { return nil }
    let base = [sender ?? (isOutgoing.map { $0 ? "我" : "对方" } ?? ""), time ?? "", body, type, links.joined(separator: "|")].joined(separator: "\u{1f}")
    return RawRecord(
        id: sha256(base) + "#\(occurrence)",
        sender: sender,
        timeText: time,
        body: body,
        messageType: type,
        links: links,
        rowPath: path,
        contextOnly: contextOnly,
        isOutgoing: isOutgoing,
        directionEvidence: evidence
    )
}

/// 增量解析缓存：键为 AX 行元素的 CFHash。实测滚动后行元素引用 100% 复用且内容不变，
/// 因此已解析过的行不必重读。为防元素被回收复用造成内容错位，缓存同时校验行高与首个文本节点。
struct RowCacheEntry {
    let rawY: CGFloat
    let height: CGFloat
    let contentGuard: String
    let record: RawRecord
}
var rowCache: [CFHashCode: RowCacheEntry] = [:]
var rowCacheHits = 0
var rowCacheMisses = 0

/// 轻量内容守卫：AX 元素可能被虚拟列表回收复用，仅凭引用、位置和行高不足以证明仍是同一条消息。
func rowContentGuard(_ row: AXUIElement) -> String {
    var values: [String] = []
    func visit(_ node: AXUIElement, depth: Int) {
        if values.count >= 2 || depth > 3 { return }
        let info = readNode(node)
        if ["AXStaticText", "AXTextArea", "AXLink"].contains(info.role), let value = info.value ?? info.title, !value.isEmpty {
            values.append(value)
        }
        for child in children(node) { visit(child, depth: depth + 1) }
    }
    visit(row, depth: 0)
    return values.joined(separator: "\u{1f}")
}

/// 从已遍历出的节点里判定方向：1=我 0=对方 nil=未知。
/// 头像位置在 AX 上会偶发返回镜像值（同一静止行实测能读到 520 与 1526），
/// 因此对候选头像连读三次取多数；不足两票就返回 nil，宁可缺失也不猜。
func directionFromNodes(_ nodes: [AXNode], midline: CGFloat?) -> Bool? {
    guard let midline = midline else { return nil }
    // 只收头像尺寸的正方形节点，排除表情、图标、滚动条。
    let avatars = nodes.filter { node in
        guard node.role == "AXImage", let f = node.frame else { return false }
        return f.width >= 18 && f.width <= 64 && abs(f.width - f.height) <= 8 && abs(f.midX - midline) >= 40
    }
    guard !avatars.isEmpty else { return nil }
    var votes = [0, 0]
    for _ in 0..<3 {
        // 取离中线最远的头像：气泡内表情离中线更近，不会被误选。
        var bestX = midline
        var bestDist: CGFloat = -1
        for a in avatars {
            guard let f = frameOf(a.element) else { continue }
            let d = abs(f.midX - midline)
            if d > bestDist { bestDist = d; bestX = f.midX }
        }
        if bestDist < 40 { continue }
        votes[bestX > midline ? 1 : 0] += 1
    }
    if votes[1] >= 2 { return true }
    if votes[0] >= 2 { return false }
    return nil
}

/// 行在表内是「视口相对坐标」：每滚动一屏，所有行的 y 整体平移（实测 +480），
/// 所以直接用 y 做缓存守卫会 100% 失配。先由若干已缓存行求出本次位移（取中位数，
/// 抗个别元素被回收），把 y 归一化成内容坐标后再比对。
func estimateShift(_ rows: [AXUIElement]) -> CGFloat {
    var deltas: [CGFloat] = []
    for row in rows {
        if deltas.count >= 9 { break }
        guard let cached = rowCache[CFHash(row)], let f = frameOf(row) else { continue }
        deltas.append(f.minY - cached.rawY)
    }
    guard !deltas.isEmpty else { return 0 }
    deltas.sort()
    return deltas[deltas.count / 2]
}

func semanticID(_ record: RawRecord) -> String {
    String(record.id.split(separator: "#", maxSplits: 1).first ?? Substring(record.id))
}

func assignStableIDs(_ records: [RawRecord]) -> [RawRecord] {
    var counts: [String: Int] = [:]
    return records.map { record in
        let base = semanticID(record)
        let occurrence = counts[base, default: 0]
        counts[base] = occurrence + 1
        return RawRecord(
            id: "\(base)#\(occurrence)", sender: record.sender, timeText: record.timeText,
            body: record.body, messageType: record.messageType, links: record.links,
            rowPath: record.rowPath, contextOnly: record.contextOnly,
            isOutgoing: record.isOutgoing, directionEvidence: record.directionEvidence
        )
    }
}

func readSnapshot(_ table: AXUIElement, midline: CGFloat?, isGroup: Bool?) -> [RawRecord] {
    var output: [RawRecord] = []
    let rows = children(table)
    let shift = estimateShift(rows)
    for (index, row) in rows.enumerated() {
        let tempPath = "row\(index)"
        guard let rowFrame = frameOf(row) else { continue }
        let key = CFHash(row)
        let normY = rowFrame.minY - shift
        let contentGuard = rowContentGuard(row)
        var parsed: RawRecord?
        if let cached = rowCache[key],
           abs(cached.rawY - normY) < 2, abs(cached.height - rowFrame.height) < 2,
           cached.contentGuard == contentGuard {
            parsed = cached.record
            rowCacheHits += 1
        } else {
            parsed = parseRow(row, path: tempPath, occurrence: 0, midline: midline, isGroup: isGroup)
            rowCacheMisses += 1
            if let p = parsed {
                rowCache[key] = RowCacheEntry(rawY: normY, height: rowFrame.height, contentGuard: contentGuard, record: p)
            }
        }
        guard let record = parsed else { continue }
        output.append(record)
    }
    return assignStableIDs(output)
}

func prependSnapshot(_ snapshot: [RawRecord], to ordered: inout [RawRecord]) -> Bool {
    if ordered.isEmpty {
        ordered = assignStableIDs(snapshot)
        return true
    }
    let incoming = snapshot.map(semanticID)
    let existing = ordered.map(semanticID)
    var overlap = 0
    let limit = min(incoming.count, existing.count)
    if limit > 0 {
        for size in stride(from: limit, through: 1, by: -1) {
            if Array(incoming.suffix(size)) == Array(existing.prefix(size)) {
                overlap = size
                break
            }
        }
    }
    if overlap > 0 {
        ordered = assignStableIDs(Array(snapshot.dropLast(overlap)) + ordered)
        return true
    }
    // 不用内容集合去重：两条完全相同的合法消息必须都保留。无重叠时保留并集并显式记 gap。
    ordered = assignStableIDs(snapshot + ordered)
    return false
}

/// 右侧信息面板显示「群成员·N」时取总人数；单聊或面板收起时返回 nil。
func detectMemberCount(_ root: AXUIElement) -> Int? {
    guard let wf = frameOf(root) else { return nil }
    let headerBottom = wf.minY + wf.height * 0.22
    let rightStart = wf.minX + wf.width * 0.72
    var found: Int?
    func visit(_ node: AXUIElement, depth: Int) {
        if found != nil || depth > 14 { return }
        let info = readNode(node)
        if info.role == "AXTable" || info.role == "AXScrollArea" { return } // 消息区不可能是成员面板
        // 只进入顶部标题区或右侧信息栏，其余分支整棵跳过。
        if let f = info.frame, !(f.minY <= headerBottom || f.minX >= rightStart) { return }
        if info.role == "AXStaticText", let value = info.value,
           value.contains("群成员"),
           let match = value.range(of: #"群成员\s*[·・:：]?\s*(\d[\d,，]*)"#, options: .regularExpression) {
            let digits = value[match].filter { $0.isNumber }
            if let count = Int(digits) { found = count; return }
        }
        for child in children(node) { visit(child, depth: depth + 1) }
    }
    visit(root, depth: 0)
    return found
}
func scrollCenter(_ scroll: AXUIElement) -> CGPoint? {
    guard let posValue = axAttr(scroll, kAXPositionAttribute),
          let sizeValue = axAttr(scroll, kAXSizeAttribute) else { return nil }
    var pos = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(posValue as! AXValue, .cgPoint, &pos),
          AXValueGetValue(sizeValue as! AXValue, .cgSize, &size) else { return nil }
    return CGPoint(x: pos.x + size.width / 2, y: pos.y + size.height / 2)
}

func performScroll(at point: CGPoint, delta: Int32) {
    let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)
    move?.post(tap: .cghidEventTap)
    usleep(50000)
    let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: delta, wheel2: 0, wheel3: 0)
    event?.location = point
    event?.post(tap: .cghidEventTap)
}

func writeCapture(_ capture: CaptureFile, prefix: String) {
    let url = URL(fileURLWithPath: NSString(string: prefix + "-raw.json").expandingTildeInPath)
    do {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        try encoder.encode(capture).write(to: url, options: .atomic)
        print("原始数据已写入：\(url.path)")
    } catch {
        fputs("写入失败：\(error.localizedDescription)\n", stderr); exit(70)
    }
}

guard let config = parseConfig() else { exit(0) }
if config.selfTestDates {
    let formatter = DateFormatter()
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = .current
    formatter.dateFormat = "yyyy-MM-dd"
    guard let reference = formatter.date(from: "2026-10-25"),
          let explicit = messageDate("2025年3月1日", relativeTo: reference),
          let rollover = messageDate("12月31日", relativeTo: reference),
          Calendar.current.component(.year, from: config.sinceDate ?? reference) == 2025,
          formatter.string(from: explicit) == "2025-03-01",
          formatter.string(from: rollover) == "2025-12-31",
          isTimeText("24/11/25 10:47"), explicitYear("24/11/25 10:47") == 2024,
          monthDay("24/11/25 10:47") == 11 * 32 + 25,
          messageDate("24/11/25 10:47",relativeTo:reference) == formatter.date(from:"2024-11-25"),
          messageDate("25/12/31 23:59",relativeTo:reference) == formatter.date(from:"2025-12-31"),
          messageDate("25/2/29 10:00",relativeTo:reference) == nil else {
        fputs("DATE_SELF_TEST_FAILED\n", stderr); exit(70)
    }
    print("DATE_SELF_TEST_OK")
    exit(0)
}
setbuf(stdout, nil) // 父进程重定向日志时也保留实时进度。
let trusted = AXIsProcessTrustedWithOptions([
    kAXTrustedCheckOptionPrompt.takeUnretainedValue(): config.prompt
] as CFDictionary)
guard trusted else { fputs("请先授予当前终端辅助功能权限，或使用 --prompt。\n", stderr); exit(2) }
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: config.bundleID).first else {
    fputs("企业微信未运行。\n", stderr); exit(3)
}

let appElement = AXUIElementCreateApplication(app.processIdentifier)
var focusedValue: CFTypeRef?
let focusedResult = AXUIElementCopyAttributeValue(appElement, kAXFocusedWindowAttribute as CFString, &focusedValue)
let root: AXUIElement
if focusedResult == .success, focusedValue != nil {
    root = focusedValue as! AXUIElement
} else if let windows = axAttr(appElement, kAXWindowsAttribute) as? [AXUIElement], let first = windows.first {
    root = first
} else {
    fputs("未找到企业微信窗口。\n", stderr); exit(4)
}

func locateMessageTableFresh() -> (AXUIElement, AXUIElement)? {
    var best: (AXUIElement, AXUIElement, Int)?
    let windowFrame = frameOf(root)
    // 左侧会话目录也是 AXTable，且行数远多于消息区；按宽度和位置排除，否则会误采目录。
    func isSidebarTable(_ frame: CGRect?) -> Bool {
        guard let tableFrame = frame, let windowFrame = windowFrame else { return false }
        return tableFrame.width <= 500 || tableFrame.origin.x < windowFrame.origin.x + windowFrame.width * 0.25
    }
    func search(_ node: AXUIElement, scroll: AXUIElement?) {
        let info = readNode(node)
        let currentScroll = info.role == "AXScrollArea" ? node : scroll
        if info.role == "AXTable" {
            // 目录表不展开，直接跳过整棵子树（原实现仍会继续递归进去）。
            if isSidebarTable(info.frame) { return }
            let rowElements = children(node).filter { readNode($0).role == "AXRow" }
            let rows = rowElements.count
            var items = (Array(rowElements.prefix(4)) + Array(rowElements.suffix(4))).flatMap(walkNodes)
            if !items.contains(where: { $0.role == "AXTextArea" }) || !items.contains(where: { $0.role == "AXStaticText" && isTimeText($0.value ?? "") }) {
                items = walkNodes(node)
            }
            let bodies = items.filter { $0.role == "AXTextArea" }.count
            let times = items.filter { $0.role == "AXStaticText" && isTimeText($0.value ?? "") }.count
            let score = bodies * 5 + times * 3 + rows
            if bodies > 0 && times > 0 && (best == nil || score > best!.2) {
                best = (node, currentScroll ?? node, score)
            }
            return
        }
        for child in children(node) { search(child, scroll: currentScroll) }
    }
    search(root, scroll: nil)
    return best.map { ($0.0, $0.1) }
}

/// 定位结果缓存：消息表格在滚动过程中引用保持有效，不必每轮重新全窗口搜索。
var cachedTable: (AXUIElement, AXUIElement)?

func locateMessageTable() -> (AXUIElement, AXUIElement)? {
    if let cached = cachedTable {
        // 轻量校验：引用仍可读且仍有行；失效才回退到全量搜索。
        if let rowCount = (axAttr(cached.0, kAXChildrenAttribute) as? [AXUIElement])?.count, rowCount > 0 {
            return cached
        }
        cachedTable = nil
    }
    cachedTable = locateMessageTableFresh()
    return cachedTable
}

guard let located = locateMessageTable() else {
    fputs("未定位到聊天消息表格。请打开一个包含文字和时间的聊天窗口后重试。\n", stderr); exit(5)
}

var ordered: [RawRecord] = []
var previousSignature = ""
var idle = 0
/// 连续多少屏没有「新内容」。客户端分页被打满时快照行数不变、行元素被复用，
/// 只靠快照签名判断会永远滚下去（实测一个 2 月会话滚了 23 屏 5700 条仍在滚）。
var contentIdle = 0
var scrollCount = 0
var sequenceGaps = 0
var stopReason = config.noScroll ? "no-scroll-test" : "max-scrolls"
var boundaryVerified = false
var previousEarliestMD: Int?
var initialNewestMD: Int?
let memberCount = detectMemberCount(root)
/// 群聊判定：右侧成员面板可见，或抽样行中多行带「姓名:」标签（面板收起时兜底）。
func looksLikeGroup(_ table: AXUIElement) -> Bool? {
    if memberCount == 1 { return false }
    if let memberCount, memberCount > 1 { return true }
    var labeled = 0
    for (i, row) in children(table).enumerated() where i < 15 {
        let cell = children(row).first { readNode($0).role == "AXCell" } ?? row
        for child in children(cell) {
            let n = readNode(child)
            if n.role == "AXStaticText", let v = n.value, v.hasSuffix(":") || v.hasSuffix("：") {
                labeled += 1
                break
            }
        }
    }
    return labeled >= 3 ? true : nil
}
var isGroupChat: Bool? = config.expectedConversationType.map { $0 == "group" }
var classificationAttempted = config.expectedConversationType != nil
print("会话成员数：\(memberCount.map(String.init) ?? "未取到（单聊或面板收起）")")

while true {
    guard let current = locateMessageTable() else { fputs("消息表格消失，停止。\n", stderr); stopReason = "table-lost"; break }
    let midline = frameOf(current.0).map { $0.origin.x + $0.width / 2 }
    if !classificationAttempted {
        isGroupChat = looksLikeGroup(current.0)
        classificationAttempted = true
        let label = isGroupChat.map { $0 ? "群聊（按姓名识别发送者）" : "单聊（按头像位置识别方向）" } ?? "未知（同时保留可观测姓名与方向，不强猜）"
        print("会话类型判定：\(label)")
    }
    // 每屏刷新一次像素快照：滚动后行位置变了，旧图不能再用
    if config.pixelDirection, let area = frameOf(current.1) ?? frameOf(current.0) {
        refreshPixelProbe(area, enabled: config.pixelDirection)
    }
    let snapshot = readSnapshot(current.0, midline: midline, isGroup: isGroupChat)
    let addedNew = prependSnapshot(snapshot, to: &ordered)
    if !addedNew, scrollCount > 0 {
        sequenceGaps += 1
        contentIdle += 1
    } else {
        contentIdle = 0
    }

    let dated = snapshot.compactMap { $0.timeText }
    let earliest = dated.compactMap(monthDay).first
    let newest = dated.compactMap(monthDay).last
    if initialNewestMD == nil { initialNewestMD = newest }
    let previousYear = dated.contains { explicitYear($0).map { $0 < config.year } ?? false }
    let crossedFromNewest = earliest != nil && initialNewestMD != nil && earliest! - initialNewestMD! > 180
    let crossedFromPrevious = earliest != nil && previousEarliestMD != nil && earliest! - previousEarliestMD! > 180
    if earliest != nil { previousEarliestMD = earliest }

    let signature = "\(snapshot.count):\(snapshot.first?.id ?? "nil"):\(snapshot.last?.id ?? "nil")"
    idle = signature == previousSignature ? idle + 1 : 0
    previousSignature = signature
    print("滚动 \(scrollCount)，当前快照 \(snapshot.count) 行，累计原始记录 \(ordered.count) 条")

    // 月日回绕只在目标起始年就是当前年时代表越界；历史或跨年区间必须继续滚。
    let currentYear = Calendar.current.component(.year, from: Date())
    if previousYear || (config.year >= currentYear && (crossedFromNewest || crossedFromPrevious)) {
        stopReason = "reached-previous-year"
        boundaryVerified = true
        break
    }
    // 已滚过完整起始日：额外保留 4 天，保证边界当天完整加载。
    let earliestDate = dated.compactMap { messageDate($0, relativeTo: Date()) }.min()
    if let sinceDate = config.sinceDate,
       let threshold = Calendar.current.date(byAdding: .day, value: -4, to: sinceDate),
       let earliestDate, earliestDate < threshold {
        stopReason = "reached-since"
        boundaryVerified = true
        break
    }
    if config.noScroll { break }
    // 客户端不再给新内容：再滚也只是白烧时间，按「未采全」处理并交给续采
    if contentIdle >= config.idleLimit { stopReason = "no-new-content"; break }
    if idle >= config.idleLimit { stopReason = "top-or-no-change"; break }  // 边界未验证：既可能是到顶，也可能是分页暂停
    if scrollCount >= config.maxScrolls { stopReason = "max-scrolls"; break }
    guard let point = scrollCenter(current.1) else { stopReason = "scroll-position-unavailable"; break }
    var displayCount: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &displayCount) == .success, displayCount > 0 else {
        stopReason = "scroll-target-offscreen"; break
    }
    var displayIDs = [CGDirectDisplayID](repeating: 0, count: Int(displayCount))
    guard CGGetActiveDisplayList(displayCount, &displayIDs, &displayCount) == .success,
          displayIDs.prefix(Int(displayCount)).contains(where: {
              CGDisplayIsAsleep($0) == 0 && CGDisplayBounds($0).contains(point)
          }) else { stopReason = "scroll-target-offscreen"; break }
    performScroll(at: point, delta: config.scrollDelta)
    scrollCount += 1
    if scrollCount % config.checkpointEvery == 0 {
        let checkpoint = CaptureFile(
            tool: "wework_scroll_capture", version: 2,
            capturedAt: ISO8601DateFormatter().string(from: Date()), targetYear: config.year,
            stopReason: "in-progress", boundaryVerified: false, scrollCount: scrollCount,
            recordCount: ordered.count, sequenceGaps: sequenceGaps, memberCount: memberCount, records: ordered
        )
        writeCapture(checkpoint, prefix: config.outputPrefix)
    }
    usleep(useconds_t(config.settleMilliseconds * 1000))
}

let capture = CaptureFile(
    tool: "wework_scroll_capture", version: 2,
    capturedAt: ISO8601DateFormatter().string(from: Date()), targetYear: config.year,
    stopReason: stopReason, boundaryVerified: boundaryVerified, scrollCount: scrollCount,
    recordCount: ordered.count, sequenceGaps: sequenceGaps, memberCount: memberCount, records: ordered
)
writeCapture(capture, prefix: config.outputPrefix)
print("行缓存命中 \(rowCacheHits)，未命中 \(rowCacheMisses)")
print("屏间序列缺口 \(sequenceGaps)")
print("停止原因：\(stopReason)；边界验证：\(boundaryVerified ? "已验证" : "未验证（不能当作已采全）")。请继续运行年份过滤与 CSV／JSONL 导出脚本。")
