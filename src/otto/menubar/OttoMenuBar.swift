// Otto menu bar companion.
//
// A thin, native window onto the Otto engine (http://localhost:<port>):
//   • badge with the number of items that need attention
//   • click → the Briefings panel: the same items the web page shows, grouped
//     the same way, each with its reasons and a picture of where it came
//     from; click a row to open the source, click the picture to see it at
//     full size, hover for dismiss / snooze
//   • right-click → the menu: refresh, clear, what needs a look (with the
//     one action each), Edit Config…, Connect Slack…, Add a Model Key…,
//     where "Briefings" opens (panel or browser), run at login, start/stop
//   • native banners for important items, pulled from /api/notifications
//
// It holds no data of its own and never talks to anything but localhost.
// Every decision about what is shown is the engine's (/api/items,
// /api/status); this file only draws it. Set-up flows run the CLI (`otto
// slack connect --json`, `otto key add --json`, `otto config --save --json`)
// with the secret — or the whole config file — on stdin; nothing sensitive
// travels over HTTP or argv.

import Cocoa
import UserNotifications

// MARK: - Config

struct OttoConfig {
    let home: String
    let python: String
    let dataDir: String
    let fallbackPort: Int

    /// The port is a config.toml setting, so it is discovered, not assumed:
    /// `OTTO_PORT` → the running engine's `engine.json` → the bundle's default.
    /// Re-read on every use; after `otto restart` on a new port the next status
    /// tick finds the engine again without the app being relaunched.
    var port: Int { OttoConfig.discoverPort(dataDir: dataDir, fallback: fallbackPort) }

    static func discoverPort(dataDir: String, fallback: Int) -> Int {
        if let p = Int(ProcessInfo.processInfo.environment["OTTO_PORT"] ?? ""), p > 0 { return p }
        if let data = FileManager.default.contents(atPath: dataDir + "/engine.json"),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let pid = json["pid"] as? Int, let port = json["port"] as? Int, port > 0,
           kill(pid_t(pid), 0) == 0 || errno == EPERM {          // that engine is alive
            return port
        }
        return fallback
    }

    static func load() -> OttoConfig {
        let info = Bundle.main.infoDictionary ?? [:]
        let env = ProcessInfo.processInfo.environment
        let fallback = (info["OttoPort"] as? Int) ?? Int(info["OttoPort"] as? String ?? "") ?? 7077
        let dataDir = env["OTTO_DATA_DIR"] ?? (NSHomeDirectory() + "/Library/Application Support/Otto")

        var home = env["OTTO_HOME"] ?? (info["OttoHome"] as? String) ?? ""
        if home.isEmpty {
            // bin/Otto.app/Contents/MacOS/OttoMenuBar → project root
            home = URL(fileURLWithPath: Bundle.main.bundlePath)
                .deletingLastPathComponent()   // bin
                .deletingLastPathComponent()   // project root
                .path
        }
        var python = env["OTTO_PYTHON"] ?? (info["OttoPython"] as? String) ?? ""
        if python.isEmpty || !FileManager.default.isExecutableFile(atPath: python) {
            let venv = home + "/.venv/bin/python3"
            python = FileManager.default.isExecutableFile(atPath: venv) ? venv : "/usr/bin/python3"
        }
        return OttoConfig(home: home, python: python, dataDir: dataDir, fallbackPort: fallback)
    }

    var base: String { "http://127.0.0.1:\(port)" }
    var pageURL: URL? { URL(string: "http://localhost:\(port)/") }
}

/// Something about Otto itself that needs a person (`/api/status` → `problems`),
/// with the one action the engine suggests — offered here as a "Fix…" button.
struct Problem {
    let key: String
    let title: String
    let detail: String
    let action: String       // permissions · slack · config · key_remove:<prefix> · restart · menubar · ""

    static func parse(_ list: [[String: Any]]) -> [Problem] {
        list.compactMap { p in
            guard let title = p["title"] as? String, !title.isEmpty else { return nil }
            let action = (p["action"] as? String) ?? ""
            if action == "menubar" { return nil }          // that one is about us, and we are evidently running
            return Problem(key: (p["key"] as? String) ?? title, title: title,
                           detail: (p["detail"] as? String) ?? "", action: action)
        }
    }

    var fixTitle: String {
        switch action {
        case "permissions": return "Fix…"
        case "slack": return "Connect Slack…"
        case "config": return "Edit Config…"
        case "restart": return "Restart"
        case _ where action.hasPrefix("key_remove:"): return "Remove key"
        default: return ""
        }
    }
}

/// What a click on the icon (and "Briefings" in the menu) does.
enum BriefingsOpens: String {
    case panel, browser
    static let key = "OttoBriefingsOpens"
    static var current: BriefingsOpens {
        get { BriefingsOpens(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .panel }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: key) }
    }
}

let allowedSchemes: Set<String> = ["http", "https", "slack", "ical"]

func openExternal(_ urlString: String) -> Bool {
    guard !urlString.isEmpty, let url = URL(string: urlString),
          allowedSchemes.contains(url.scheme?.lowercased() ?? "") else { return false }
    return NSWorkspace.shared.open(url)
}

// MARK: - Payload (/api/items)

struct Reason {
    let label: String
    let tone: String
    let detail: String
}

struct BriefingItem {
    let id: String
    let title: String
    let line: String
    let summary: String
    let why: [Reason]
    let level: String          // "critical" / "high" / ""
    let urgency: Double
    let channel: String
    let sender: String
    let time: String
    let url: String
    let externalURL: String
    let screenshotPath: String
    let actionItems: [String]

    /// "#eng · alice · 2h ago" — the sender is left out when the channel already
    /// names them (a DM with alice, your own notes-to-self).
    var meta: String {
        let bare = sender.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "@#"))
        let who = (!bare.isEmpty && channel.lowercased().contains(bare)) ? "" : sender
        return [channel, who, time].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct ItemGroup {
    let key: String
    let label: String
    let items: [BriefingItem]
}

struct RadarRow {
    let id: String
    let what: String
    let who: String
    let channel: String
    let due: String
    let overdue: Bool
    let soon: Bool
    let url: String
    let kind: String
}

struct RadarSection {
    let key: String
    let heading: String
    let glyph: String
    let rows: [RadarRow]
}

struct Recurring {
    let id: String
    let label: String
    let channel: String
    let lateBy: String
}

/// One line under Worth knowing: a ▲ heads-up, a ◇ prediction, or • one of
/// the radar's own notes — with the id the engine dismisses it by.
struct Note {
    let id: String
    let kind: String        // heads_up · prediction · attention
    let text: String
}

struct Payload {
    let headline: String
    let why: String
    let digest: String
    let headsUp: [String]
    let predictions: [String]
    /// Heads-ups and predictions with ids (`worth_knowing.notes`); what the
    /// Worth knowing view lists above the radar.
    let notes: [Note]
    let generatedAtHuman: String
    let count: Int
    let importantCount: Int
    let groups: [ItemGroup]
    let radar: [RadarSection]
    let recurringLate: [Recurring]
    let blockedSources: [String]

    static let worthKnowingSub = "Heads-ups, what's likely next, your radar, and anything about Otto itself — none of it a notification."
    static let worthKnowingEmpty = "Nothing more to know right now."

    /// Everything the Worth knowing view shows: notes, radar rows, late series.
    var worthKnowingCount: Int {
        notes.count + radar.reduce(0) { $0 + $1.rows.count } + recurringLate.count
    }
    var worthKnowingIds: Set<String> {
        var ids = Set<String>()
        for n in notes where !n.id.isEmpty { ids.insert(n.id) }
        for sec in radar { for r in sec.rows { ids.insert(r.id) } }
        for rec in recurringLate { ids.insert(rec.id) }
        return ids
    }

    static func parse(_ json: [String: Any]) -> Payload {
        func s(_ d: [String: Any], _ k: String) -> String { (d[k] as? String) ?? "" }
        func b(_ d: [String: Any], _ k: String) -> Bool { (d[k] as? Bool) ?? false }
        func list(_ d: [String: Any], _ k: String) -> [[String: Any]] { (d[k] as? [[String: Any]]) ?? [] }

        let groups: [ItemGroup] = list(json, "groups").map { g in
            ItemGroup(key: s(g, "key"), label: s(g, "label"), items: list(g, "items").map { it in
                BriefingItem(
                    id: s(it, "id"), title: s(it, "title"), line: s(it, "line"), summary: s(it, "summary"),
                    why: list(it, "why").map { Reason(label: s($0, "label"), tone: s($0, "tone"), detail: s($0, "detail")) },
                    level: s(it, "level"), urgency: (it["urgency_score"] as? Double) ?? 0,
                    channel: s(it, "channel"), sender: s(it, "sender"), time: s(it, "time"),
                    url: s(it, "url"), externalURL: s(it, "external_url"),
                    screenshotPath: s(it, "screenshot_path"),
                    actionItems: (it["action_items"] as? [String]) ?? []
                )
            })
        }
        let radarDict = (json["radar"] as? [String: Any]) ?? [:]
        let radar: [RadarSection] = list(radarDict, "sections").map { sec in
            RadarSection(key: s(sec, "key"), heading: s(sec, "heading"), glyph: s(sec, "glyph"), rows: list(sec, "rows").map { r in
                RadarRow(id: s(r, "id"), what: s(r, "what"), who: s(r, "who"), channel: s(r, "channel"), due: s(r, "due"),
                         overdue: b(r, "overdue"), soon: b(r, "soon"), url: s(r, "url"), kind: s(r, "kind"))
            })
        }
        let recurring: [Recurring] = list(radarDict, "recurring").compactMap { p in
            let late = s(p, "late_by")
            return late.isEmpty ? nil : Recurring(id: s(p, "id"), label: s(p, "label"), channel: s(p, "channel"), lateBy: late)
        }
        let digest = (json["digest"] as? [String: Any]) ?? [:]
        let headsUp = (digest["heads_up"] as? [String]) ?? []
        let predictions = list(digest, "predictions").map { s($0, "note") }.filter { !$0.isEmpty }
        var notes: [Note] = list((json["worth_knowing"] as? [String: Any]) ?? [:], "notes")
            .map { Note(id: s($0, "id"), kind: s($0, "kind"), text: s($0, "text")) }
            .filter { !$0.text.isEmpty }
        if notes.isEmpty && json["worth_knowing"] == nil {
            // An engine from before notes had ids: list them anyway (no ✕ per row).
            notes = headsUp.prefix(3).map { Note(id: "", kind: "heads_up", text: $0) }
                + predictions.prefix(3).map { Note(id: "", kind: "prediction", text: $0) }
        }
        return Payload(
            headline: s(json, "headline"), why: s(json, "why"),
            digest: s(digest, "text"),
            headsUp: headsUp,
            predictions: predictions,
            notes: notes,
            generatedAtHuman: s(json, "generated_at_human"),
            count: (json["count"] as? Int) ?? 0,
            importantCount: (json["important_count"] as? Int) ?? 0,
            groups: groups, radar: radar, recurringLate: recurring,
            blockedSources: (json["blocked_sources"] as? [String]) ?? []
        )
    }

    /// Every id the payload carries — items, notes, radar rows, late series.
    var allIds: Set<String> {
        var ids = Set<String>()
        for g in groups { for it in g.items { ids.insert(it.id) } }
        ids.formUnion(worthKnowingIds)
        return ids
    }

    /// The same briefing minus `ids` (what was just dismissed or snoozed here
    /// and the engine may not have dropped yet), counts adjusted to match.
    func without(_ ids: Set<String>) -> Payload {
        if ids.isEmpty { return self }
        var removed = 0, removedImportant = 0
        let groups: [ItemGroup] = self.groups.compactMap { g in
            let kept = g.items.filter { !ids.contains($0.id) }
            let gone = g.items.count - kept.count
            removed += gone
            if g.key == "important" { removedImportant += gone }
            return kept.isEmpty ? nil : ItemGroup(key: g.key, label: g.label, items: kept)
        }
        let radar: [RadarSection] = self.radar.compactMap { sec in
            let kept = sec.rows.filter { !ids.contains($0.id) }
            return kept.isEmpty ? nil : RadarSection(key: sec.key, heading: sec.heading, glyph: sec.glyph, rows: kept)
        }
        return Payload(
            headline: headline, why: why, digest: digest, headsUp: headsUp, predictions: predictions,
            notes: notes.filter { $0.id.isEmpty || !ids.contains($0.id) },
            generatedAtHuman: generatedAtHuman,
            count: max(0, count - removed), importantCount: max(0, importantCount - removedImportant),
            groups: groups, radar: radar, recurringLate: recurringLate.filter { !ids.contains($0.id) },
            blockedSources: blockedSources
        )
    }
}

// MARK: - Status icon

/// The menu bar glyph: a plain ring (an "O"), and beside it — smaller than the
/// menu bar's own text — the number of items that need you, or "!" when a
/// source is waiting on a macOS permission. Drawn as a template image so it
/// follows the bar's light/dark tint and dims with `appearsDisabled`.
enum StatusIcon {
    static let height: CGFloat = 18
    static let ring: CGFloat = 14
    static let stroke: CGFloat = 1.75
    static let gap: CGFloat = 3
    static let font = NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .semibold)

    static func label(count: Int, alert: Bool) -> String {
        if count > 0 { return count > 99 ? "99+" : "\(count)" }
        return alert ? "!" : ""
    }

    static func image(count: Int, alert: Bool) -> NSImage {
        let text = label(count: count, alert: alert)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
        let textSize = text.isEmpty ? .zero : (text as NSString).size(withAttributes: attrs)
        let inset: CGFloat = 1                                  // keeps the stroke off the edge
        let width = ceil(inset + ring + (text.isEmpty ? 0 : gap + textSize.width) + inset)
        let img = NSImage(size: NSSize(width: width, height: height), flipped: false) { _ in
            let circle = NSRect(x: inset + stroke / 2, y: (height - ring) / 2 + stroke / 2,
                                width: ring - stroke, height: ring - stroke)
            let path = NSBezierPath(ovalIn: circle)
            path.lineWidth = stroke
            NSColor.black.setStroke()
            path.stroke()
            if !text.isEmpty {
                // Centre the digits' cap height on the ring's centre, not the
                // glyph box, so the number sits level with the shape.
                let baseline = height / 2 - font.capHeight / 2
                // draw(at:) takes the bottom-left of the line box; the baseline
                // sits |descender| above it.
                (text as NSString).draw(at: NSPoint(x: inset + ring + gap, y: baseline + font.descender - font.leading),
                                        withAttributes: attrs)
            }
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = text.isEmpty ? "Otto" : (alert && count == 0 ? "Otto — needs a permission" : "Otto — \(text) need you")
        return img
    }
}

// MARK: - Look

enum Look {
    static let width: CGFloat = 392
    static let maxHeight: CGFloat = 680
    static let pad: CGFloat = 14
    static let thumbSize = NSSize(width: 84, height: 52)

    static func toneColor(_ tone: String) -> NSColor {
        switch tone {
        case "you": return .systemBlue
        case "directive": return .systemBlue
        case "alert": return .systemRed
        case "due": return .systemOrange
        case "focus": return .systemIndigo
        case "opportunity": return .systemGreen
        default: return .secondaryLabelColor
        }
    }

    static func levelColor(_ level: String) -> NSColor? {
        switch level {
        case "critical": return .systemRed
        case "high": return .systemOrange
        default: return nil
        }
    }

    /// Text column widths, so wrapping labels know where to wrap (AppKit does
    /// not infer it): the panel minus row insets, the accent gutter, the gap and
    /// the right-hand column (thumbnail, or just the hover buttons).
    static let headerTextWidth = width - 2 * pad
    static let noteTextWidth = width - 16 - 8 - 6 - 12 - 6 - 8
    static let rightColumnMin: CGFloat = 46
    static func rowTextWidth(hasThumb: Bool) -> CGFloat {
        width - 16 - 14 - 10 - (hasThumb ? thumbSize.width : rightColumnMin) - 8
    }

    static func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular,
                      color: NSColor = .labelColor, lines: Int = 1, width: CGFloat = 0) -> NSTextField {
        let l = NSTextField(wrappingLabelWithString: text)
        l.font = .systemFont(ofSize: size, weight: weight)
        l.textColor = color
        l.maximumNumberOfLines = lines
        // Truncating-tail turns wrapping off; wrap by words and let the cell
        // ellipsise the last visible line instead.
        l.lineBreakMode = lines > 1 ? .byWordWrapping : .byTruncatingTail
        l.cell?.wraps = lines > 1
        l.cell?.truncatesLastVisibleLine = true
        l.isSelectable = false
        if width > 0 { l.preferredMaxLayoutWidth = width }
        l.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        l.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return l
    }
}

/// A small rounded tag: the "why this is for you" chip.
final class ChipView: NSView {
    init(_ reason: Reason) {
        super.init(frame: .zero)
        wantsLayer = true
        let color = Look.toneColor(reason.tone)
        layer?.cornerRadius = 5
        layer?.backgroundColor = (reason.tone == "you" ? color : color.withAlphaComponent(0.14)).cgColor
        let l = Look.label(reason.label, size: 10, weight: .semibold, color: reason.tone == "you" ? .white : color)
        l.setContentCompressionResistancePriority(.required, for: .horizontal)
        l.setContentHuggingPriority(.required, for: .horizontal)
        l.translatesAutoresizingMaskIntoConstraints = false
        addSubview(l)
        NSLayoutConstraint.activate([
            l.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            l.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
            l.topAnchor.constraint(equalTo: topAnchor, constant: 2.5),
            l.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2.5),
        ])
        toolTip = reason.detail.isEmpty ? nil : reason.detail
        setContentCompressionResistancePriority(.required, for: .horizontal)
    }
    required init?(coder: NSCoder) { fatalError() }
}

/// A button that is just a glyph, shown on hover.
func glyphButton(_ symbol: String, fallback: String, tip: String, target: AnyObject?, action: Selector) -> NSButton {
    let b = NSButton(title: "", target: target, action: action)
    b.isBordered = false
    b.bezelStyle = .inline
    if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: tip) {
        b.image = img
        b.imagePosition = .imageOnly
        b.contentTintColor = .secondaryLabelColor
    } else {
        b.title = fallback
    }
    b.toolTip = tip
    b.setButtonType(.momentaryChange)
    b.translatesAutoresizingMaskIntoConstraints = false
    b.widthAnchor.constraint(equalToConstant: 22).isActive = true
    b.heightAnchor.constraint(equalToConstant: 20).isActive = true
    return b
}

// MARK: - Thumbnails

/// Pictures of the source are read straight from the engine's screenshot
/// directory (a local file), never copied; the full-size viewer shows the
/// original pixels.
final class ImageStore {
    static let shared = ImageStore()
    private var cache: [String: NSImage] = [:]
    private let queue = DispatchQueue(label: "otto.images", qos: .userInitiated)

    func image(at path: String, completion: @escaping (NSImage?) -> Void) {
        if path.isEmpty { completion(nil); return }
        if let img = cache[path] { completion(img); return }
        queue.async {
            let img = NSImage(contentsOfFile: path)
            DispatchQueue.main.async {
                if let img = img { self.cache[path] = img }
                if self.cache.count > 60 { self.cache.removeAll() }
                completion(img)
            }
        }
    }

    /// Pixel dimensions of the bitmap, whatever DPI the file claims.
    static func pixelSize(_ image: NSImage) -> NSSize {
        for rep in image.representations {
            if rep.pixelsWide > 0 && rep.pixelsHigh > 0 {
                return NSSize(width: rep.pixelsWide, height: rep.pixelsHigh)
            }
        }
        return image.size
    }
}

// MARK: - Rows

protocol RowDelegate: AnyObject {
    func row(_ row: ItemRowView, open item: BriefingItem)
    func row(_ row: ItemRowView, enlarge item: BriefingItem, image: NSImage)
    func row(_ row: ItemRowView, dismiss item: BriefingItem)
    func row(_ row: ItemRowView, snooze item: BriefingItem, hours: Double)
}

final class ItemRowView: NSView {
    let item: BriefingItem
    weak var delegate: RowDelegate?
    private let hoverButtons = NSStackView()
    private var thumb: NSImageView?
    private var thumbImage: NSImage?
    private var tracking: NSTrackingArea?
    private var hovered = false { didSet { needsDisplay = true; hoverButtons.isHidden = !hovered } }

    init(item: BriefingItem) {
        self.item = item
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 9
        translatesAutoresizingMaskIntoConstraints = false
        build()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 4
        text.translatesAutoresizingMaskIntoConstraints = false
        let textWidth = Look.rowTextWidth(hasThumb: !item.screenshotPath.isEmpty)

        let title = Look.label(item.title, size: 13, weight: .semibold, lines: 2, width: textWidth)
        text.addArrangedSubview(title)

        if !item.why.isEmpty {
            let chips = NSStackView()
            chips.orientation = .horizontal
            chips.spacing = 4
            chips.alignment = .centerY
            for r in item.why.prefix(3) { chips.addArrangedSubview(ChipView(r)) }
            chips.setClippingResistancePriority(.defaultLow, for: .horizontal)
            text.addArrangedSubview(chips)
        }
        if !item.line.isEmpty && item.line != item.title {
            text.addArrangedSubview(Look.label(item.line, size: 12, color: .secondaryLabelColor, lines: 2, width: textWidth))
        }
        let metaText = item.meta
        if !metaText.isEmpty {
            let m = Look.label(metaText, size: 11, color: .tertiaryLabelColor, width: textWidth)
            text.addArrangedSubview(m)
        }

        let accent = NSView()
        accent.wantsLayer = true
        accent.layer?.cornerRadius = 1.5
        accent.layer?.backgroundColor = (Look.levelColor(item.level) ?? .clear).cgColor
        accent.translatesAutoresizingMaskIntoConstraints = false

        addSubview(accent)
        addSubview(text)

        let right = NSStackView()
        right.orientation = .vertical
        right.alignment = .trailing
        right.spacing = 6
        right.translatesAutoresizingMaskIntoConstraints = false
        addSubview(right)

        hoverButtons.orientation = .horizontal
        hoverButtons.spacing = 0
        hoverButtons.isHidden = true
        hoverButtons.addArrangedSubview(glyphButton("zzz", fallback: "1h", tip: "Snooze for an hour", target: self, action: #selector(snoozeHour)))
        hoverButtons.addArrangedSubview(glyphButton("xmark", fallback: "✕", tip: "Dismiss", target: self, action: #selector(dismiss)))
        right.addArrangedSubview(hoverButtons)

        if !item.screenshotPath.isEmpty {
            let iv = NSImageView()
            iv.wantsLayer = true
            iv.layer?.cornerRadius = 6
            iv.layer?.masksToBounds = true
            iv.layer?.borderWidth = 0.5
            iv.layer?.borderColor = NSColor.separatorColor.cgColor
            iv.imageScaling = .scaleProportionallyUpOrDown
            iv.imageAlignment = .alignTop
            iv.translatesAutoresizingMaskIntoConstraints = false
            iv.widthAnchor.constraint(equalToConstant: Look.thumbSize.width).isActive = true
            iv.heightAnchor.constraint(equalToConstant: Look.thumbSize.height).isActive = true
            iv.toolTip = "Where this came from — click to enlarge"
            right.addArrangedSubview(iv)
            thumb = iv
            ImageStore.shared.image(at: item.screenshotPath) { [weak self] img in
                guard let self = self, let img = img else { return }
                self.thumbImage = img
                iv.image = img
            }
        }

        NSLayoutConstraint.activate([
            accent.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            accent.topAnchor.constraint(equalTo: topAnchor, constant: 10),
            accent.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -10),
            accent.widthAnchor.constraint(equalToConstant: 3),
            text.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            text.topAnchor.constraint(equalTo: topAnchor, constant: 9),
            text.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -9),
            right.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            right.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            right.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -8),
            text.trailingAnchor.constraint(equalTo: right.leadingAnchor, constant: -10),
        ])
        text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        right.setContentHuggingPriority(.required, for: .horizontal)
        right.setContentCompressionResistancePriority(.required, for: .horizontal)
        // The hover buttons appear in this column: reserve it so text never reflows on hover.
        right.widthAnchor.constraint(greaterThanOrEqualToConstant: Look.rightColumnMin).isActive = true
        toolTip = item.url.isEmpty ? nil : "Open in " + (item.url.hasPrefix("slack") || item.url.contains("slack.com") ? "Slack" : "the source")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseEntered(with event: NSEvent) { hovered = true }
    override func mouseExited(with event: NSEvent) { hovered = false }

    override func draw(_ dirtyRect: NSRect) {
        if hovered {
            NSColor.labelColor.withAlphaComponent(0.06).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill()
        }
    }

    override func mouseUp(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if let iv = thumb, iv.frame.contains(convert(p, to: iv.superview) ), let img = thumbImage {
            delegate?.row(self, enlarge: item, image: img)
            return
        }
        delegate?.row(self, open: item)
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        func add(_ title: String, _ action: Selector?, enabled: Bool = true) {
            let mi = NSMenuItem(title: title, action: action, keyEquivalent: "")
            mi.target = self
            mi.isEnabled = enabled && action != nil
            menu.addItem(mi)
        }
        menu.autoenablesItems = false
        add("Open", #selector(openSource), enabled: !item.url.isEmpty)
        if !item.externalURL.isEmpty { add("Open link", #selector(openLink)) }
        if thumbImage != nil { add("Show where it came from", #selector(enlarge)) }
        menu.addItem(.separator())
        add("Snooze for an hour", #selector(snoozeHour))
        add("Snooze until tomorrow", #selector(snoozeTomorrow))
        add("Dismiss", #selector(dismiss))
        if !item.why.isEmpty {
            menu.addItem(.separator())
            let head = NSMenuItem(title: "Why this is for you", action: nil, keyEquivalent: "")
            head.isEnabled = false
            menu.addItem(head)
            for r in item.why {
                let mi = NSMenuItem(title: "   " + r.label + (r.detail.isEmpty ? "" : " — " + r.detail), action: nil, keyEquivalent: "")
                mi.isEnabled = false
                menu.addItem(mi)
            }
        }
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    @objc private func openSource() { delegate?.row(self, open: item) }
    @objc private func openLink() { _ = openExternal(item.externalURL) }
    @objc private func enlarge() { if let img = thumbImage { delegate?.row(self, enlarge: item, image: img) } }
    @objc private func dismiss() { delegate?.row(self, dismiss: item) }
    @objc private func snoozeHour() { delegate?.row(self, snooze: item, hours: 1) }
    @objc private func snoozeTomorrow() {
        // 8:00 tomorrow, local time — the same as the web page's "Tomorrow".
        var comps = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        comps.day = (comps.day ?? 0) + 1
        comps.hour = 8; comps.minute = 0
        let target = Calendar.current.date(from: comps) ?? Date().addingTimeInterval(16 * 3600)
        delegate?.row(self, snooze: item, hours: max(1, target.timeIntervalSinceNow / 3600))
    }
}

/// One radar line: "○ alice: review the runner image · #eng · due tomorrow".
final class RadarRowView: NSView {
    let row: RadarRow
    var onDismiss: ((RadarRow) -> Void)?
    private let x: NSButton
    private var tracking: NSTrackingArea?

    init(row: RadarRow, glyph: String, glyphColor: NSColor = .tertiaryLabelColor) {
        self.row = row
        self.x = glyphButton("xmark", fallback: "✕", tip: "Dismiss", target: nil, action: #selector(dismissTapped))
        super.init(frame: .zero)
        x.target = self
        x.isHidden = true
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.cornerRadius = 6

        // A note's glyph (▲ ◇ •) is a mark and reads bold; a radar row's is a bullet.
        let g = Look.label(glyph, size: 12, weight: row.kind == "note" ? .bold : .regular, color: glyphColor)
        g.setContentCompressionResistancePriority(.required, for: .horizontal)
        g.setContentHuggingPriority(.required, for: .horizontal)
        let lead = (row.who.isEmpty || row.who == "you" || !["promise", "ask", "open_call"].contains(row.kind)) ? "" : row.who + ": "
        let text = NSMutableAttributedString()
        if !lead.isEmpty {
            text.append(NSAttributedString(string: lead, attributes: [.font: NSFont.systemFont(ofSize: 12, weight: .semibold), .foregroundColor: NSColor.labelColor]))
        }
        text.append(NSAttributedString(string: row.what, attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.labelColor]))
        let metaBits = [row.channel].filter { !$0.isEmpty }.joined(separator: " · ")
        if !metaBits.isEmpty {
            text.append(NSAttributedString(string: "  " + metaBits, attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.tertiaryLabelColor]))
        }
        let what = NSTextField(labelWithAttributedString: text)
        what.maximumNumberOfLines = row.kind == "note" ? 3 : 2      // a heads-up is a sentence, a radar row a phrase
        what.lineBreakMode = .byTruncatingTail
        what.cell?.wraps = true
        what.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let due = Look.label(row.due, size: 11, weight: row.overdue || row.soon ? .semibold : .regular,
                             color: row.overdue ? .systemRed : (row.soon ? .systemOrange : .secondaryLabelColor))
        due.setContentCompressionResistancePriority(.required, for: .horizontal)
        due.setContentHuggingPriority(.required, for: .horizontal)

        let h = NSStackView(views: [g, what, due, x])
        h.orientation = .horizontal
        h.alignment = .firstBaseline
        h.spacing = 6
        h.translatesAutoresizingMaskIntoConstraints = false
        addSubview(h)
        NSLayoutConstraint.activate([
            h.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            h.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            h.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            h.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4),
        ])
        toolTip = row.url.isEmpty ? nil : "Open in the source"
    }
    required init?(coder: NSCoder) { fatalError() }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }
    override func mouseEntered(with event: NSEvent) { x.isHidden = false; layer?.backgroundColor = NSColor.labelColor.withAlphaComponent(0.05).cgColor }
    override func mouseExited(with event: NSEvent) { x.isHidden = true; layer?.backgroundColor = nil }
    override func mouseUp(with event: NSEvent) { _ = openExternal(row.url) }
    @objc private func dismissTapped() { onDismiss?(row) }
}

// MARK: - Full-size viewer

/// The original screenshot at its own resolution (or scaled down to fit the
/// screen when it is bigger), floating above everything, gone on click or Esc.
final class ImageViewerPanel: NSPanel {
    private static var current: ImageViewerPanel?
    private var openURL = ""

    static func show(image: NSImage, caption: String, url: String) {
        current?.orderOut(nil)
        let panel = ImageViewerPanel(image: image, caption: caption, url: url)
        current = panel
        panel.alphaValue = 0
        panel.makeKeyAndOrderFront(nil)
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.16
            panel.animator().alphaValue = 1
        }
    }

    static func hide() {
        guard let p = current else { return }
        current = nil
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.12
            p.animator().alphaValue = 0
        }, completionHandler: { p.orderOut(nil) })
    }

    private init(image: NSImage, caption: String, url: String) {
        let screen = NSScreen.main ?? NSScreen.screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let scale = screen?.backingScaleFactor ?? 2
        let px = ImageStore.pixelSize(image)
        // 1 image pixel = 1 device pixel when it fits; otherwise scale down (never up).
        var size = NSSize(width: px.width / scale, height: px.height / scale)
        let maxW = visible.width * 0.9, maxH = visible.height * 0.9 - 56
        let k = min(1, min(maxW / max(size.width, 1), maxH / max(size.height, 1)))
        size = NSSize(width: floor(size.width * k), height: floor(size.height * k))
        let chrome: CGFloat = 44
        let frame = NSRect(x: visible.midX - size.width / 2 - 12, y: visible.midY - (size.height + chrome) / 2 - 12,
                           width: size.width + 24, height: size.height + chrome + 24)
        super.init(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        openURL = url
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        isMovableByWindowBackground = true
        hidesOnDeactivate = false

        let root = NSVisualEffectView(frame: NSRect(origin: .zero, size: frame.size))
        root.material = .hudWindow
        root.blendingMode = .behindWindow
        root.state = .active
        root.wantsLayer = true
        root.layer?.cornerRadius = 14
        root.layer?.masksToBounds = true
        contentView = root

        let iv = ClickThroughImageView(frame: NSRect(x: 12, y: 12, width: size.width, height: size.height))
        iv.image = image
        image.size = NSSize(width: px.width / scale, height: px.height / scale)
        iv.imageScaling = .scaleProportionallyDown
        iv.wantsLayer = true
        iv.layer?.cornerRadius = 8
        iv.layer?.masksToBounds = true
        iv.onClick = { ImageViewerPanel.hide() }
        root.addSubview(iv)

        let cap = Look.label(caption, size: 12, weight: .medium, color: .white)
        cap.frame = NSRect(x: 16, y: frame.height - 34, width: frame.width - 200, height: 18)
        root.addSubview(cap)

        if !url.isEmpty {
            let open = NSButton(title: "Open ↗", target: self, action: #selector(openTapped))
            open.bezelStyle = .inline
            open.font = .systemFont(ofSize: 11, weight: .semibold)
            open.frame = NSRect(x: frame.width - 128, y: frame.height - 36, width: 76, height: 22)
            root.addSubview(open)
        }
        let close = NSButton(title: "✕", target: self, action: #selector(closeTapped))
        close.bezelStyle = .inline
        close.frame = NSRect(x: frame.width - 44, y: frame.height - 36, width: 30, height: 22)
        root.addSubview(close)
    }

    override var canBecomeKey: Bool { true }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { ImageViewerPanel.hide() } else { super.keyDown(with: event) }   // Esc
    }
    override func resignKey() { super.resignKey(); ImageViewerPanel.hide() }
    @objc private func closeTapped() { ImageViewerPanel.hide() }
    @objc private func openTapped() { _ = openExternal(openURL); ImageViewerPanel.hide() }
}

final class ClickThroughImageView: NSImageView {
    var onClick: (() -> Void)?
    override func mouseUp(with event: NSEvent) { onClick?() }
}

// MARK: - Panel

protocol PanelDelegate: AnyObject {
    func panelWantsRefresh()
    func panelWantsBrowser()
    func panelWantsMenu(from view: NSView)
    func panelWantsClear(count: Int)
    func panelWantsStart()
    func panelWantsConfig()
    func panelWantsFix(_ problem: Problem)
    func panelDidOpenSomething()
    func panelPost(_ path: String, form: [String: String], completion: @escaping ([String: Any]?) -> Void)
}

/// A scroll view whose bar is always a real bar. Overlay scrollers vanish
/// when the mouse rests, and a list that can scroll then looks like a list
/// that cannot; the legacy style keeps the bar (and its track) visible for
/// as long as there is something to scroll to. AppKit re-applies the system
/// preference on every appearance change, hence the override rather than a
/// one-time assignment.
final class LegacyScrollView: NSScrollView {
    override var scrollerStyle: NSScroller.Style {
        get { .legacy }
        set { super.scrollerStyle = .legacy }
    }
}

/// The Briefings panel: header (headline + digest), the grouped rows, a footer.
///
/// Two views share it. *Briefings* is the notifications and nothing else. *Worth
/// knowing* — what needs a look about Otto itself, the digest's heads-ups and
/// predictions, the radar's open loops and dates, the series that keep coming
/// back — is one click away in the footer, never mixed into the list, and
/// clears on its own.
final class PanelController: NSViewController, RowDelegate {
    enum Showing { case briefings, worthKnowing }

    weak var delegate: PanelDelegate?
    private(set) var payload: Payload?
    private(set) var showing: Showing = .briefings
    var offline = false
    var refreshing = false
    /// What needs a person about Otto itself (from /api/status); rendered as
    /// quiet rows with the one thing to do.
    var problems: [Problem] = []

    private let scroll = LegacyScrollView()
    private let stack = NSStackView()
    private var clearButton: NSButton!
    private var notesButton: NSButton!
    private var backButton: NSButton!
    private var browserButton: NSButton!
    /// Header, separators and footer: measured on their own in `fit()`, because
    /// the root view's `fittingSize` does not shrink once its frame has grown.
    private var chrome: [NSView] = []
    private let headTitle = Look.label("Briefings", size: 15, weight: .bold)
    private let headStatus = Look.label("", size: 11, color: .tertiaryLabelColor)
    private let headline = Look.label("", size: 13, color: .secondaryLabelColor, lines: 2, width: Look.headerTextWidth)
    private let digestLabel = Look.label("", size: 12.5, lines: 4, width: Look.headerTextWidth)
    private let footerLeft = Look.label("", size: 11, color: .tertiaryLabelColor)
    private var refreshButton: NSButton!
    private var alsoNoticedShown = false
    private var heightConstraint: NSLayoutConstraint?
    /// Dismissed or snoozed from this panel; kept out of every render until
    /// the engine's own payload no longer carries them (so a status tick
    /// during the fade cannot bring a row back). Snoozed items return on
    /// their own once the engine shows them again.
    private var hiddenIds = Set<String>()
    /// Rows mid-fade; a second click on one of them does nothing.
    private var leaving = Set<ObjectIdentifier>()

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: Look.width, height: 400))
        root.translatesAutoresizingMaskIntoConstraints = false
        root.widthAnchor.constraint(equalToConstant: Look.width).isActive = true

        // Header
        refreshButton = glyphButton("arrow.clockwise", fallback: "↻", tip: "Refresh now", target: self, action: #selector(refreshTapped))
        let gear = glyphButton("gearshape", fallback: "⚙", tip: "Edit Config… (every setting explained; keys under [keys])", target: self, action: #selector(configTapped))
        let more = glyphButton("ellipsis.circle", fallback: "⋯", tip: "More", target: self, action: #selector(moreTapped))
        headTitle.setContentHuggingPriority(.required, for: .horizontal)
        let headRow = NSStackView(views: [headTitle, headStatus, refreshButton, gear, more])
        headRow.orientation = .horizontal
        headRow.alignment = .centerY
        headRow.spacing = 6
        headRow.setCustomSpacing(10, after: headTitle)
        headRow.translatesAutoresizingMaskIntoConstraints = false
        headStatus.alignment = .right

        digestLabel.font = .systemFont(ofSize: 12.5)
        digestLabel.textColor = .labelColor

        let header = NSStackView(views: [headRow, headline, digestLabel])
        header.orientation = .vertical
        header.alignment = .leading
        header.spacing = 6
        header.translatesAutoresizingMaskIntoConstraints = false
        header.edgeInsets = NSEdgeInsets(top: 12, left: Look.pad, bottom: 10, right: Look.pad)

        let sep = NSBox()
        sep.boxType = .separator
        sep.translatesAutoresizingMaskIntoConstraints = false

        // Body
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        stack.edgeInsets = NSEdgeInsets(top: 6, left: 8, bottom: 8, right: 8)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let doc = FlippedView()
        doc.translatesAutoresizingMaskIntoConstraints = false
        doc.addSubview(stack)
        scroll.documentView = doc
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.scrollerStyle = .legacy          // a bar that stays (see LegacyScrollView)
        scroll.autohidesScrollers = true        // …but only while there is something to scroll
        scroll.verticalScroller?.controlSize = .small
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.contentView.postsBoundsChangedNotifications = true

        // Footer: the count · [Worth knowing · N] [browser glyph] [Clear] — or,
        // inside Worth knowing: [‹ Briefings] … [Clear]. "Open in Browser" stays
        // spelled out in the ⋯ menu; here the glyph leaves room for the count.
        let back = NSButton(title: "‹ Briefings", target: self, action: #selector(backTapped))
        back.bezelStyle = .inline
        back.font = .systemFont(ofSize: 11, weight: .medium)
        back.isHidden = true
        backButton = back
        let notes = NSButton(title: "Worth knowing", target: self, action: #selector(notesTapped))
        notes.bezelStyle = .inline
        notes.font = .systemFont(ofSize: 11, weight: .medium)
        notes.toolTip = Payload.worthKnowingSub
        notes.isHidden = true
        notesButton = notes
        let browser = glyphButton("safari", fallback: "↗", tip: "Open in browser", target: self, action: #selector(browserTapped))
        browserButton = browser
        let clear = NSButton(title: "Clear", target: self, action: #selector(clearTapped))
        clear.bezelStyle = .inline
        clear.font = .systemFont(ofSize: 11, weight: .medium)
        clear.toolTip = "Dismiss everything on the briefing (one click; items stay in Otto's memory)"
        clearButton = clear
        let footer = NSStackView(views: [back, footerLeft, notes, browser, clear])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = 8
        footer.edgeInsets = NSEdgeInsets(top: 6, left: Look.pad, bottom: 8, right: Look.pad)
        footer.translatesAutoresizingMaskIntoConstraints = false
        // The count takes whatever width the buttons leave (and truncates first
        // when there is not enough), so the buttons always sit at the right edge.
        footer.distribution = .fill
        footerLeft.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .horizontal)
        footerLeft.lineBreakMode = .byTruncatingTail
        for b in [back, notes, browser, clear] {
            b.setContentCompressionResistancePriority(.required, for: .horizontal)
            b.setContentHuggingPriority(.required, for: .horizontal)
        }
        let sep2 = NSBox()
        sep2.boxType = .separator
        sep2.translatesAutoresizingMaskIntoConstraints = false

        for v in [header, sep, scroll, sep2, footer] as [NSView] { root.addSubview(v) }
        chrome = [header, sep, sep2, footer]
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: root.topAnchor),
            header.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            header.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sep.topAnchor.constraint(equalTo: header.bottomAnchor),
            sep.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: sep.bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            sep2.topAnchor.constraint(equalTo: scroll.bottomAnchor),
            sep2.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            sep2.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            footer.topAnchor.constraint(equalTo: sep2.bottomAnchor),
            footer.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            stack.topAnchor.constraint(equalTo: doc.topAnchor),
            stack.leadingAnchor.constraint(equalTo: doc.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: doc.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: doc.bottomAnchor),
            // The clip view, not the scroll view: when a legacy scroll bar takes
            // room (mouse users, "always show scroll bars"), rows must shrink
            // with it instead of running under it.
            doc.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
        ])
        let hc = scroll.heightAnchor.constraint(equalToConstant: 200)
        hc.priority = .defaultHigh
        hc.isActive = true
        heightConstraint = hc
        view = root
    }

    // MARK: rendering

    func render(_ payload: Payload?) {
        // The first click renders before the popover has shown the view; the
        // buttons are created in loadView(), so load it now rather than trap.
        if !isViewLoaded { _ = view }
        self.payload = payload
        for v in stack.arrangedSubviews { stack.removeArrangedSubview(v); v.removeFromSuperview() }
        // Ids the engine has dropped need no hiding any more.
        if let all = payload?.allIds { hiddenIds.formIntersection(all) }

        guard let p = payload?.without(hiddenIds) else {
            showing = .briefings
            headline.stringValue = offline ? "Otto isn't running." : "Loading…"
            headline.textColor = .secondaryLabelColor
            digestLabel.stringValue = ""
            digestLabel.isHidden = true
            headStatus.stringValue = ""
            if offline {
                let start = NSButton(title: "Start Otto", target: self, action: #selector(startTapped))
                start.bezelStyle = .rounded
                stack.addArrangedSubview(pad(start))
            }
            footerLeft.stringValue = ""
            footerLeft.isHidden = false
            for b in [clearButton, notesButton, backButton, browserButton] { b?.isHidden = true }
            fit()
            return
        }

        headStatus.stringValue = refreshing ? "Refreshing…" : shortTime(p.generatedAtHuman)
        refreshButton.isEnabled = !refreshing
        if showing == .worthKnowing {
            renderWorthKnowing(p)
            fit()
            return
        }

        let quiet = p.count == 0          // nothing needs you
        headline.stringValue = p.headline
        headline.textColor = quiet ? .labelColor : .secondaryLabelColor
        let digest = quiet || p.digest.isEmpty || p.digest.lowercased() == p.headline.lowercased() ? "" : p.digest
        digestLabel.stringValue = digest
        digestLabel.isHidden = digest.isEmpty
        clearButton.isHidden = quiet
        clearButton.toolTip = "Dismiss everything on the briefing (one click; items stay in Otto's memory)"
        browserButton.isHidden = false
        backButton.isHidden = true
        footerLeft.isHidden = false
        let known = worthKnowingTotal(p)
        notesButton.title = "Worth knowing · \(known)"
        notesButton.isHidden = known == 0

        if quiet {
            // Nothing in the list, and nothing else either — no "still worth
            // knowing", no ⚠︎ about Otto itself: all of that lives behind the
            // footer button now.
            let text = known > 0
                ? "Nothing needs you right now. Heads-ups, your radar and notes about Otto itself are under Worth knowing, below."
                : "Otto keeps reading — new things land here within a minute."
            let line = Look.label(text, size: 12, color: .secondaryLabelColor, lines: 3, width: Look.headerTextWidth)
            stack.addArrangedSubview(pad(line, top: 22, left: 10, bottom: 22))
        }

        for g in p.groups {
            if g.key == "also_noticed" && !alsoNoticedShown {
                let b = NSButton(title: "Also noticed · \(g.items.count) more", target: self, action: #selector(showAlsoNoticed))
                b.bezelStyle = .inline
                b.font = .systemFont(ofSize: 11, weight: .semibold)
                b.contentTintColor = .secondaryLabelColor
                stack.addArrangedSubview(pad(b, top: 6))
                continue
            }
            stack.addArrangedSubview(groupLabel(g.label, count: g.items.count))
            for it in g.items {
                let row = ItemRowView(item: it)
                row.delegate = self
                stack.addArrangedSubview(row)
                row.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -16).isActive = true
            }
        }

        footerLeft.stringValue = quiet ? "Up to date" : "\(p.count) item\(p.count == 1 ? "" : "s")" + (p.importantCount > 0 ? " · \(p.importantCount) need\(p.importantCount == 1 ? "s" : "") you" : "")
        fit()
    }

    /// What the Worth knowing view holds: notes and radar rows (clearable) plus
    /// what needs a look about Otto itself (goes away when fixed).
    private func worthKnowingTotal(_ p: Payload) -> Int { p.worthKnowingCount + min(problems.count, 3) }

    /// The Worth knowing view: ⚠︎ About Otto first (each with the one button
    /// that fixes it), then the notes, then the radar — every note and row with
    /// its own ✕, one Clear for all of those, ‹ Briefings to go back. Nothing
    /// here needs you, so nothing here is orange except a glyph.
    private func renderWorthKnowing(_ p: Payload) {
        let known = worthKnowingTotal(p)
        headline.stringValue = "Worth knowing" + (known > 0 ? " · \(known)" : "")
        headline.textColor = .labelColor
        digestLabel.stringValue = Payload.worthKnowingSub
        digestLabel.isHidden = false
        footerLeft.stringValue = ""            // stays, empty: it is what keeps Clear on the right
        footerLeft.isHidden = false
        backButton.isHidden = false
        notesButton.isHidden = true
        browserButton.isHidden = true
        clearButton.isHidden = false
        clearButton.isEnabled = p.worthKnowingCount > 0
        clearButton.toolTip = "Dismiss every note and radar row here (one click; nothing is deleted from Otto's memory)"

        if known == 0 {
            let line = Look.label(Payload.worthKnowingEmpty, size: 12, color: .secondaryLabelColor, lines: 2, width: Look.headerTextWidth)
            stack.addArrangedSubview(pad(line, top: 22, left: 10, bottom: 22))
            return
        }
        if !problems.isEmpty {
            stack.addArrangedSubview(pad(Look.label("ABOUT OTTO", size: 10, weight: .semibold, color: .tertiaryLabelColor), top: 8, left: 8))
            for problem in problems.prefix(3) { addRow(problemRow(problem)) }
        }
        if !p.notes.isEmpty {
            stack.addArrangedSubview(pad(Look.label("NOTES", size: 10, weight: .semibold, color: .tertiaryLabelColor), top: 8, left: 8))
        }
        for n in p.notes {
            let (glyph, color): (String, NSColor) = n.kind == "heads_up" ? ("▲", .systemOrange)
                : n.kind == "prediction" ? ("◇", .systemIndigo) : ("•", .tertiaryLabelColor)
            let row = RadarRow(id: n.id, what: n.text, who: "", channel: "", due: "", overdue: false, soon: false, url: "", kind: "note")
            let rv = RadarRowView(row: row, glyph: glyph, glyphColor: color)
            if n.id.isEmpty { rv.onDismiss = nil } else { rv.onDismiss = { [weak self] r in self?.dismissRadar(r, view: rv) } }
            stack.addArrangedSubview(rv)
            rv.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -16).isActive = true
        }
        addRadar(p)
    }

    /// Switch views. Called from the footer buttons and when the popover opens
    /// (it always opens on Briefings).
    func show(_ what: Showing) {
        guard showing != what else { return }
        showing = what
        render(payload)
    }

    /// Clear inside Worth knowing: every note and radar row leaves at once (the
    /// items stay); the engine is told with one call and the view shows the
    /// result now rather than on the next tick.
    private func clearWorthKnowing() {
        guard let p = payload?.without(hiddenIds), p.worthKnowingCount > 0 else { return }
        hiddenIds.formUnion(p.worthKnowingIds)
        render(payload)
        delegate?.panelPost("/api/clear", form: ["scope": "notes"]) { _ in }
    }

    /// One thing about Otto itself that needs a person, and the button that does it.
    private func problemRow(_ problem: Problem) -> NSView {
        let g = Look.label("⚠︎", size: 10, weight: .bold, color: .systemOrange)
        g.setContentHuggingPriority(.required, for: .horizontal)
        g.setContentCompressionResistancePriority(.required, for: .horizontal)
        let text = problem.detail.isEmpty ? problem.title : problem.title + " — " + problem.detail
        let t = Look.label(text, size: 12, color: .labelColor, lines: 3, width: Look.noteTextWidth - 90)
        var views: [NSView] = [g, t]
        if !problem.fixTitle.isEmpty {
            let b = NSButton(title: problem.fixTitle, target: self, action: #selector(fixTapped(_:)))
            b.bezelStyle = .inline
            b.font = .systemFont(ofSize: 11, weight: .semibold)
            b.tag = problems.firstIndex(where: { $0.key == problem.key }) ?? 0
            b.setContentHuggingPriority(.required, for: .horizontal)
            b.setContentCompressionResistancePriority(.required, for: .horizontal)
            views.append(b)
        }
        let h = NSStackView(views: views)
        h.orientation = .horizontal
        h.alignment = .firstBaseline
        h.spacing = 6
        return pad(h, top: 4, left: 8, bottom: 2)
    }

    /// The radar's sections (To do · Waiting on · Nobody has taken this · Coming
    /// up · Unread in Slack) and the late series, inside the Worth knowing view.
    private func addRadar(_ p: Payload) {
        guard !p.radar.isEmpty || !p.recurringLate.isEmpty else { return }
        let perSection = 8
        for sec in p.radar {
            let head = Look.label(sec.heading.uppercased(), size: 10, weight: .semibold, color: .tertiaryLabelColor)
            stack.addArrangedSubview(pad(head, top: 8, left: 8))
            for r in sec.rows.prefix(perSection) {
                let rv = RadarRowView(row: r, glyph: sec.glyph)
                rv.onDismiss = { [weak self] row in self?.dismissRadar(row, view: rv) }
                stack.addArrangedSubview(rv)
                rv.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -16).isActive = true
            }
            if sec.rows.count > perSection {
                stack.addArrangedSubview(pad(Look.label("+\(sec.rows.count - perSection) more in the browser", size: 11, color: .tertiaryLabelColor), left: 26))
            }
        }
        if !p.recurringLate.isEmpty {
            stack.addArrangedSubview(pad(Look.label("KEEPS COMING BACK", size: 10, weight: .semibold, color: .tertiaryLabelColor), top: 8, left: 8))
            for rec in p.recurringLate.prefix(3) {
                let row = RadarRow(id: rec.id, what: rec.label, who: "", channel: rec.channel, due: "late " + rec.lateBy,
                                   overdue: false, soon: true, url: "", kind: "series")
                let rv = RadarRowView(row: row, glyph: "▪")
                rv.onDismiss = { [weak self] r in self?.dismissRadar(r, view: rv) }
                stack.addArrangedSubview(rv)
                rv.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -16).isActive = true
            }
        }
    }

    private func groupLabel(_ text: String, count: Int?) -> NSView {
        let l = Look.label(text.uppercased(), size: 10.5, weight: .bold, color: .secondaryLabelColor)
        let c = Look.label(count.map { String($0) } ?? "", size: 10.5, weight: .medium, color: .tertiaryLabelColor)
        let h = NSStackView(views: [l, c])
        h.orientation = .horizontal
        h.spacing = 5
        return pad(h, top: 10, left: 8, bottom: 2)
    }

    /// Add a row that spans the panel's width (constraints need a common ancestor first).
    private func addRow(_ v: NSView) {
        stack.addArrangedSubview(v)
        v.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -16).isActive = true
    }

    private func pad(_ v: NSView, top: CGFloat = 0, left: CGFloat = 6, bottom: CGFloat = 0) -> NSView {
        let w = NSView()
        w.translatesAutoresizingMaskIntoConstraints = false
        v.translatesAutoresizingMaskIntoConstraints = false
        w.addSubview(v)
        NSLayoutConstraint.activate([
            v.leadingAnchor.constraint(equalTo: w.leadingAnchor, constant: left),
            v.trailingAnchor.constraint(lessThanOrEqualTo: w.trailingAnchor, constant: -6),
            v.topAnchor.constraint(equalTo: w.topAnchor, constant: top),
            v.bottomAnchor.constraint(equalTo: w.bottomAnchor, constant: -bottom),
        ])
        return w
    }

    private func shortTime(_ human: String) -> String {
        // "Saturday, September 12 2026 · 04:49 AM" → "Updated 04:49 AM"
        if let r = human.range(of: " · ") { return "Updated " + String(human[r.upperBound...]) }
        return human
    }

    /// Size the panel to its content, up to a screen-aware maximum.
    func fit() {
        view.layoutSubtreeIfNeeded()
        stack.layoutSubtreeIfNeeded()
        let screenH = (NSScreen.main?.visibleFrame.height ?? 900) - 40
        let chromeH = headerAndFooterHeight()
        let maxBody = min(Look.maxHeight, screenH) - chromeH
        let needed = stack.fittingSize.height + 2
        let body = max(48, min(needed, maxBody))
        heightConstraint?.constant = body
        // A layout root grows to satisfy its constraints but never shrinks by
        // itself (a shorter view after a taller one kept the old height, the
        // header stretching to fill it): size it to what it needs, explicitly.
        let total = chromeH + body
        if view.window == nil { view.setFrameSize(NSSize(width: Look.width, height: total)) }
        view.layoutSubtreeIfNeeded()
        preferredContentSize = NSSize(width: Look.width, height: total)
    }

    /// Header + separators + footer, each measured on its own.
    private func headerAndFooterHeight() -> CGFloat {
        chrome.reduce(0) { $0 + $1.fittingSize.height }
    }

    /// For `--preview`: "scroller legacy shown 12pt" — is the bar a real bar, and is it up.
    var scrollerReport: String {
        scroll.tile()
        scroll.reflectScrolledClipView(scroll.contentView)
        let bar = scroll.verticalScroller
        let overflow = (scroll.documentView?.frame.height ?? 0) > scroll.contentView.bounds.height
        let shown = bar.map { !$0.isHidden && $0.frame.width > 0 } ?? false
        return "scroller \(scroll.scrollerStyle == .legacy ? "legacy" : "overlay") "
            + (overflow ? (shown ? "shown" : "MISSING") : (shown ? "shown-without-overflow" : "hidden, nothing to scroll"))
            + String(format: " %.0fpt knob %.2f", bar?.frame.width ?? 0, bar?.knobProportion ?? 0)
    }

    // MARK: row delegate

    func row(_ row: ItemRowView, open item: BriefingItem) {
        let target = item.url.isEmpty ? item.externalURL : item.url
        guard openExternal(target) else { return }
        delegate?.panelPost("/api/feedback", form: ["kind": "open", "id": item.id]) { _ in }
        delegate?.panelDidOpenSomething()
    }

    func row(_ row: ItemRowView, enlarge item: BriefingItem, image: NSImage) {
        delegate?.panelPost("/api/feedback", form: ["kind": "expand", "id": item.id]) { _ in }
        ImageViewerPanel.show(image: image, caption: item.meta, url: item.url)
    }

    func row(_ row: ItemRowView, dismiss item: BriefingItem) {
        guard leave(row, id: item.id) else { return }
        delegate?.panelPost("/api/dismiss", form: ["id": item.id]) { _ in }
    }

    func row(_ row: ItemRowView, snooze item: BriefingItem, hours: Double) {
        guard leave(row, id: item.id) else { return }
        delegate?.panelPost("/api/snooze", form: ["id": item.id, "hours": String(format: "%.2f", hours)]) { _ in }
    }

    private func dismissRadar(_ row: RadarRow, view: NSView) {
        guard leave(view, id: row.id) else { return }
        delegate?.panelPost("/api/dismiss", form: ["id": row.id]) { _ in }
    }

    /// A row fades and folds; the counts follow on the next status tick.
    /// Returns false when the row is already on its way out.
    ///
    /// The list can be rebuilt while the fade runs (a status tick, the reply
    /// to the dismiss itself), which detaches the row; the completion only
    /// removes what is still in the stack — asking NSStackView to remove a
    /// view it no longer holds is an assertion, and that took the whole
    /// menu bar app down.
    @discardableResult
    private func leave(_ v: NSView, id: String) -> Bool {
        let key = ObjectIdentifier(v)
        guard !leaving.contains(key) else { return false }
        leaving.insert(key)
        hiddenIds.insert(id)
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.18
            v.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self = self else { return }
            self.leaving.remove(key)
            if self.stack.arrangedSubviews.contains(v) { self.stack.removeArrangedSubview(v) }
            if v.superview != nil { v.removeFromSuperview() }
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.2
                ctx.allowsImplicitAnimation = true
                self.fit()
            }
        })
        return true
    }

    /// For `--preview --dismiss-first`: dismiss the first item row and rebuild
    /// the list at once, the exact sequence that used to crash. False if there
    /// is no item row to dismiss.
    func rehearseDismiss() -> Bool {
        func firstRow() -> ItemRowView? { stack.arrangedSubviews.compactMap { $0 as? ItemRowView }.first }
        if firstRow() == nil { alsoNoticedShown = true; render(payload) }   // rows may all be folded away
        guard let rv = firstRow() else { return false }
        row(rv, dismiss: rv.item)
        render(payload)
        return true
    }

    /// For `--preview --worth-knowing --clear`: press Clear inside Worth knowing.
    func rehearseClearWorthKnowing() { clearWorthKnowing() }

    // MARK: buttons

    @objc private func refreshTapped() { delegate?.panelWantsRefresh() }
    @objc private func browserTapped() { delegate?.panelWantsBrowser() }
    @objc private func moreTapped(_ sender: NSButton) { delegate?.panelWantsMenu(from: sender) }
    @objc private func configTapped() { delegate?.panelWantsConfig() }
    @objc private func clearTapped() {
        if showing == .worthKnowing { clearWorthKnowing() } else { delegate?.panelWantsClear(count: payload?.count ?? 0) }
    }
    @objc private func notesTapped() { show(.worthKnowing) }
    @objc private func backTapped() { show(.briefings) }
    @objc private func startTapped() { delegate?.panelWantsStart() }
    @objc private func showAlsoNoticed() { alsoNoticedShown = true; render(payload) }
    @objc private func fixTapped(_ sender: NSButton) {
        guard sender.tag >= 0, sender.tag < problems.count else { return }
        delegate?.panelWantsFix(problems[sender.tag])
    }

    /// Clear was clicked: show the cleared briefing at once (the engine's own
    /// payload follows in milliseconds) — items gone, counts zero, headline calm.
    func showCleared() {
        guard let p = payload else { return }
        render(Payload(headline: "All clear — just keeping you up to date.", why: "", digest: "",
                       headsUp: p.headsUp, predictions: p.predictions, notes: p.notes, generatedAtHuman: p.generatedAtHuman,
                       count: 0, importantCount: 0, groups: [], radar: p.radar, recurringLate: p.recurringLate,
                       blockedSources: p.blockedSources))
    }
}

/// Top-left origin so the stack fills the scroll view from the top.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

// MARK: - Running the CLI

/// One `otto … --json` run: JSON lines out (one per step), an optional secret
/// in on stdin. stdout is parsed line by line and delivered on the main
/// queue; stderr is dropped. The secret is written to the pipe and the pipe
/// closed — it never appears in argv, the environment, a URL or a log.
final class OttoTask {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var buffer = Data()
    private var sent = false
    var onLine: (([String: Any]) -> Void)?
    var onExit: ((Int32) -> Void)?

    init(config: OttoConfig, args: [String]) {
        process.executableURL = URL(fileURLWithPath: config.python)
        process.arguments = ["-m", "otto.cli"] + args
        process.environment = OttoTask.environment(config)
        process.currentDirectoryURL = URL(fileURLWithPath: config.home)
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
    }

    static func environment(_ config: OttoConfig) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = config.home + "/src"
        env["OTTO_HOME"] = config.home
        env["PYTHONUNBUFFERED"] = "1"
        return env
    }

    /// False when the interpreter could not be started.
    @discardableResult
    func start() -> Bool {
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }      // EOF: stop being called
            DispatchQueue.main.async { self?.consume(data) }
        }
        process.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.output.fileHandleForReading.readabilityHandler = nil
                self.consume(Data())
                self.onExit?(p.terminationStatus)
            }
        }
        do { try process.run() } catch { return false }
        return true
    }

    /// Hand the CLI its one line of input and close the pipe (once).
    func send(_ line: String) {
        sendAll(line + "\n")
    }

    /// Hand the CLI a whole document on stdin (the config editor's text) and close the pipe (once).
    func sendAll(_ text: String) {
        guard !sent else { return }
        sent = true
        if let data = text.data(using: .utf8) { input.fileHandleForWriting.write(data) }
        try? input.fileHandleForWriting.close()
    }

    /// Complete lines are cut out of the buffer *before* any callback runs:
    /// a callback may show a modal (a nested run loop), during which more
    /// output can arrive and re-enter here.
    private func consume(_ data: Data) {
        buffer.append(data)
        var lines: [Data] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            lines.append(buffer.subdata(in: buffer.startIndex..<nl))
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        if data.isEmpty && !buffer.isEmpty {         // a last line without its newline
            lines.append(buffer)
            buffer.removeAll()
        }
        for line in lines {
            if let json = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { onLine?(json) }
        }
    }
}

// MARK: - Config editor

/// Edit Config…: config.toml in a window of its own — the file's text in a
/// monospaced view, Save and Cancel. Loading and saving go through the CLI
/// (`otto config --json` / `otto config --save --json`, the text on stdin),
/// which creates the file if needed, refuses to write anything that does not
/// parse (the line is named; the text stays here to be fixed), keeps the
/// file mode 0600 because `[keys]` may hold keys, and asks the running
/// engine to reload right away. Nothing here touches the network.
final class ConfigEditor: NSObject, NSWindowDelegate, NSTextViewDelegate {
    private let config: OttoConfig
    private var window: NSWindow?
    private var textView: NSTextView!
    private var status: NSTextField!
    private var pathLabel: NSTextField!
    private var saveButton: NSButton!
    private var cancelButton: NSButton!
    private var loaded = ""
    private var task: OttoTask?
    /// Called after a successful save so the status line and problems refresh.
    var onSaved: (() -> Void)?
    /// Opens the file in the default text editor instead (`otto config`).
    var openExternally: (() -> Void)?

    init(config: OttoConfig) {
        self.config = config
        super.init()
    }

    func show() {
        if window == nil { build() }
        guard let window = window else { return }
        load()
        NSApp.activate(ignoringOtherApps: true)
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(textView)
    }

    private var isDirty: Bool { textView.string != loaded }

    /// `--preview-config`: the window's content with *text* in place and the
    /// engine's verdict on it shown, without the CLI, without showing anything.
    func previewView(text: String, path: String, error: String, unknown: [String]) -> NSView {
        if window == nil { build() }
        loaded = text
        textView.string = text
        pathLabel.stringValue = path
        textView.isEditable = true
        saveButton.isEnabled = true
        showVerdict(error: error, unknown: unknown, prefix: "",
                    clean: "Every setting is listed with the value in force. Keys go under [keys].")
        window?.layoutIfNeeded()
        return window!.contentView!
    }

    private func build() {
        // Wide enough for the template's `key = value   # explanation` lines
        // (about 118 monospace columns) before they wrap.
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 640),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Otto — Config"
        w.minSize = NSSize(width: 520, height: 320)
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.setFrameAutosaveName("OttoConfigEditor")

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 940, height: 540))
        tv.isRichText = false
        tv.allowsUndo = true
        tv.usesFindBar = true
        tv.isIncrementalSearchingEnabled = true
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.isContinuousSpellCheckingEnabled = false
        tv.isGrammarCheckingEnabled = false
        tv.smartInsertDeleteEnabled = false
        tv.font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
        tv.textColor = .labelColor
        tv.backgroundColor = .textBackgroundColor
        tv.textContainerInset = NSSize(width: 10, height: 12)
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(width: 940, height: CGFloat.greatestFiniteMagnitude)
        tv.delegate = self
        scroll.documentView = tv
        textView = tv

        let bar = NSView()
        bar.translatesAutoresizingMaskIntoConstraints = false
        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false

        pathLabel = Look.label("", size: 11, color: .tertiaryLabelColor)
        pathLabel.isSelectable = true
        pathLabel.translatesAutoresizingMaskIntoConstraints = false
        status = Look.label("", size: 12, color: .secondaryLabelColor, lines: 2, width: 420)
        status.translatesAutoresizingMaskIntoConstraints = false

        let external = NSButton(title: "Open in Editor", target: self, action: #selector(openInEditor))
        external.bezelStyle = .rounded
        external.controlSize = .regular
        external.toolTip = "The same file in your default text editor"
        external.translatesAutoresizingMaskIntoConstraints = false
        cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        cancelButton.translatesAutoresizingMaskIntoConstraints = false
        saveButton = NSButton(title: "Save", target: self, action: #selector(save))
        saveButton.bezelStyle = .rounded
        saveButton.keyEquivalent = "s"
        saveButton.keyEquivalentModifierMask = [.command]
        saveButton.toolTip = "Save (⌘S) — the engine picks the change up right away"
        saveButton.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        w.contentView = content
        content.addSubview(scroll)
        content.addSubview(divider)
        content.addSubview(bar)
        bar.addSubview(pathLabel)
        bar.addSubview(status)
        bar.addSubview(external)
        bar.addSubview(cancelButton)
        bar.addSubview(saveButton)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: divider.topAnchor),
            divider.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            divider.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            divider.bottomAnchor.constraint(equalTo: bar.topAnchor),
            bar.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            bar.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            bar.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            bar.heightAnchor.constraint(equalToConstant: 74),

            pathLabel.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 16),
            pathLabel.topAnchor.constraint(equalTo: bar.topAnchor, constant: 10),
            pathLabel.trailingAnchor.constraint(lessThanOrEqualTo: bar.trailingAnchor, constant: -16),
            status.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 16),
            status.topAnchor.constraint(equalTo: pathLabel.bottomAnchor, constant: 4),
            status.trailingAnchor.constraint(lessThanOrEqualTo: external.leadingAnchor, constant: -12),

            saveButton.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -14),
            saveButton.centerYAnchor.constraint(equalTo: status.centerYAnchor),
            saveButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 84),
            cancelButton.trailingAnchor.constraint(equalTo: saveButton.leadingAnchor, constant: -8),
            cancelButton.centerYAnchor.constraint(equalTo: saveButton.centerYAnchor),
            cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 84),
            external.trailingAnchor.constraint(equalTo: cancelButton.leadingAnchor, constant: -16),
            external.centerYAnchor.constraint(equalTo: saveButton.centerYAnchor),
        ])
        window = w
    }

    // MARK: load / save through the CLI

    private func load() {
        guard task == nil else { return }
        textView.isEditable = false
        saveButton.isEnabled = false
        cancelButton.title = "Cancel"
        setStatus("Loading…", tone: .secondaryLabelColor)
        let t = OttoTask(config: config, args: ["config", "--json"])
        task = t
        var got = false
        t.onLine = { [weak self] line in
            guard let self = self else { return }
            got = true
            let text = (line["text"] as? String) ?? ""
            self.pathLabel.stringValue = (line["path"] as? String) ?? ""
            self.loaded = text
            self.textView.string = text
            self.textView.isEditable = true
            self.saveButton.isEnabled = true
            self.textView.undoManager?.removeAllActions()
            if (line["ok"] as? Bool) == false {
                self.setStatus((line["error"] as? String) ?? "could not read the file", tone: .systemRed)
            } else {
                self.showVerdict(error: (line["error"] as? String) ?? "", unknown: (line["unknown_keys"] as? [String]) ?? [],
                                 prefix: "", clean: "Every setting is listed with the value in force. Keys go under [keys].")
            }
        }
        t.onExit = { [weak self] _ in
            self?.task = nil
            if !got { self?.setStatus("Otto's command line could not be run — is the project still where it was installed?", tone: .systemRed) }
        }
        if !t.start() {
            task = nil
            setStatus("Otto's command line could not be run.", tone: .systemRed)
        }
    }

    @objc private func save() {
        guard task == nil, textView.isEditable else { return }
        let text = textView.string
        saveButton.isEnabled = false
        setStatus("Saving…", tone: .secondaryLabelColor)
        let t = OttoTask(config: config, args: ["config", "--save", "--json"])
        task = t
        var got = false
        t.onLine = { [weak self] line in
            guard let self = self else { return }
            got = true
            let saved = (line["saved"] as? Bool) ?? false
            let error = (line["error"] as? String) ?? ""
            let unknown = (line["unknown_keys"] as? [String]) ?? []
            if saved {
                self.loaded = text
                let applied = (line["applied"] as? Bool) ?? false
                let when = applied ? "Otto is reloading it now." : "Otto reads it when it next runs."
                self.showVerdict(error: error, unknown: unknown, prefix: "Saved — but ",
                                 clean: "Saved. " + when + " Port and logging apply after a restart.")
                self.cancelButton.title = "Done"
                self.onSaved?()
            } else {
                self.setStatus("Not saved — " + (error.isEmpty ? "the file did not parse" : error) + ". Fix the line and save again.", tone: .systemRed)
            }
        }
        t.onExit = { [weak self] _ in
            guard let self = self else { return }
            self.task = nil
            self.saveButton.isEnabled = true
            if !got { self.setStatus("Not saved — Otto's command line could not be run.", tone: .systemRed) }
        }
        if t.start() {
            t.sendAll(text)
        } else {
            task = nil
            saveButton.isEnabled = true
            setStatus("Not saved — Otto's command line could not be run.", tone: .systemRed)
        }
    }

    /// The engine's own verdict on the file, said once: a wrong type keeps the
    /// default for that setting, an unknown name is ignored — both worth fixing.
    private func showVerdict(error: String, unknown: [String], prefix: String, clean: String) {
        var bits: [String] = []
        if !error.isEmpty { bits.append(error + " (the default stands for that one)") }
        if !unknown.isEmpty {
            bits.append("unknown setting" + (unknown.count == 1 ? " " : "s ") + unknown.joined(separator: ", ") + " — check the spelling")
        }
        if bits.isEmpty {
            setStatus(clean, tone: .secondaryLabelColor)
        } else {
            setStatus(prefix + bits.joined(separator: "; "), tone: .systemOrange)
        }
    }

    private func setStatus(_ text: String, tone: NSColor) {
        status.stringValue = text
        status.textColor = tone
    }

    @objc private func openInEditor() {
        openExternally?()
    }

    @objc private func cancel() {
        window?.performClose(nil)
    }

    // MARK: NSTextViewDelegate / NSWindowDelegate

    func textDidChange(_ notification: Notification) {
        if cancelButton.title == "Done" { cancelButton.title = "Cancel" }
        if status.textColor == .systemRed || status.stringValue.hasPrefix("Saved") {
            setStatus("", tone: .secondaryLabelColor)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard textView.isEditable, isDirty else { return true }
        let alert = NSAlert()
        alert.messageText = "Discard your changes?"
        alert.informativeText = "config.toml has not been saved."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Discard")
        alert.addButton(withTitle: "Keep Editing")
        return alert.runModal() == .alertFirstButtonReturn
    }
}

// MARK: - App delegate

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSUserNotificationCenterDelegate, PanelDelegate, NSPopoverDelegate {
    let config = OttoConfig.load()
    var statusItem: NSStatusItem!
    var statusTimer: Timer?
    var notifyTimer: Timer?
    let popover = NSPopover()
    let panel = PanelController()
    var outsideClickMonitor: Any?

    var offline = true
    var itemCount = 0
    var importantCount = 0
    var lastUpdated: TimeInterval = 0
    var renderedUpdated: TimeInterval = -1
    var refreshing = false
    var lastError = ""
    /// Sources the engine could not read because of a macOS permission (name → reason).
    var blockedSources: [(String, String)] = []
    /// What needs a person about Otto itself, from the engine (core/health.py).
    var problems: [Problem] = []
    /// Set-up flows in progress (Connect Slack…, Add a Model Key…); one at a time.
    var runningTask: OttoTask?
    var configEditor: ConfigEditor?

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ note: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        NSUserNotificationCenter.default.delegate = self

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = StatusIcon.image(count: 0, alert: false)
            button.imagePosition = .imageOnly
            button.title = ""
            button.toolTip = "Otto — Briefings"
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        popover.contentViewController = panel
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        panel.delegate = self
        updateStatusButton()

        checkStatus()
        statusTimer = Timer.scheduledTimer(timeInterval: 10, target: self, selector: #selector(checkStatus), userInfo: nil, repeats: true)
        notifyTimer = Timer.scheduledTimer(timeInterval: 15, target: self, selector: #selector(pullNotifications), userInfo: nil, repeats: true)
        pullNotifications()

        // Wake from sleep → refresh immediately instead of waiting for the next tick.
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(checkStatus),
                                                          name: NSWorkspace.didWakeNotification, object: nil)
    }

    // MARK: HTTP

    private func request(_ path: String, method: String = "GET", form: [String: String] = [:],
                         completion: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: config.base + path) else { completion(nil); return }
        var req = URLRequest(url: url, timeoutInterval: 4)
        req.httpMethod = method
        req.setValue("localhost:\(config.port)", forHTTPHeaderField: "Host")
        if method == "POST" {
            req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
            let body = form.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "")" }
                .joined(separator: "&")
            req.httpBody = body.data(using: .utf8)
        }
        URLSession.shared.dataTask(with: req) { data, _, error in
            guard error == nil, let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                DispatchQueue.main.async { completion(nil) }
                return
            }
            DispatchQueue.main.async { completion(json) }
        }.resume()
    }

    func panelPost(_ path: String, form: [String: String], completion: @escaping ([String: Any]?) -> Void) {
        request(path, method: "POST", form: form) { [weak self] json in
            completion(json)
            if path == "/api/dismiss" || path == "/api/snooze" {
                // Loopback answers in milliseconds; let the row finish fading
                // before the badge and list catch up.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { self?.checkStatus() }
            }
        }
    }

    // MARK: status

    @objc func checkStatus() {
        request("/api/status") { [weak self] json in
            guard let self = self else { return }
            guard let json = json else {
                self.offline = true
                self.panel.offline = true
                if self.popover.isShown { self.panel.render(nil) }
                self.updateStatusButton()
                return
            }
            let wasOffline = self.offline
            self.offline = false
            self.panel.offline = false
            self.itemCount = json["items"] as? Int ?? 0
            self.importantCount = json["critical_items"] as? Int ?? 0
            self.lastUpdated = json["last_updated"] as? TimeInterval ?? 0
            self.refreshing = json["refreshing"] as? Bool ?? false
            self.lastError = json["last_error"] as? String ?? ""
            self.blockedSources = []
            if let statuses = json["source_status"] as? [[String: Any]] {
                for st in statuses {
                    let ok = st["ok"] as? Bool ?? false
                    let err = st["error"] as? String ?? ""
                    let name = st["source"] as? String ?? "source"
                    if !ok && !err.isEmpty && err != "app not open" {
                        self.blockedSources.append((name.capitalized, err))
                    }
                }
            }
            self.problems = Problem.parse((json["problems"] as? [[String: Any]]) ?? [])
            self.panel.problems = self.problems
            self.updateStatusButton()
            if self.popover.isShown {
                self.panel.refreshing = self.refreshing
                if wasOffline || self.lastUpdated != self.renderedUpdated { self.loadPanel() }
                else { self.panel.render(self.panel.payload) }
            }
        }
    }

    @objc func pullNotifications() {
        request("/api/notifications") { [weak self] json in
            guard let self = self, let list = json?["notifications"] as? [[String: Any]], !list.isEmpty else { return }
            var ids: [String] = []
            for n in list {
                guard let id = n["id"] as? String else { continue }
                ids.append(id)
                self.showBanner(id: id, title: n["title"] as? String ?? "Otto", body: n["body"] as? String ?? "",
                                url: n["url"] as? String ?? "", imagePath: n["image"] as? String ?? "")
            }
            self.request("/api/notifications/ack", method: "POST", form: ["ids": ids.joined(separator: ",")]) { _ in }
        }
    }

    // MARK: status button

    private func relativeAge() -> String {
        guard lastUpdated > 0 else { return "never" }
        let secs = Int(Date().timeIntervalSince1970 - lastUpdated)
        if secs < 60 { return "just now" }
        if secs < 3600 { return "\(secs / 60)m ago" }
        return "\(secs / 3600)h ago"
    }

    func updateStatusButton() {
        guard let button = statusItem.button else { return }
        if offline {
            button.image = StatusIcon.image(count: 0, alert: false)
            button.appearsDisabled = true
            button.toolTip = "Otto isn't running — click to start"
        } else {
            button.appearsDisabled = false
            button.image = StatusIcon.image(count: importantCount, alert: !blockedSources.isEmpty)
            if importantCount > 0 {
                button.toolTip = "\(importantCount) thing\(importantCount == 1 ? "" : "s") need\(importantCount == 1 ? "s" : "") you — click for Briefings"
            } else if !blockedSources.isEmpty {
                button.toolTip = "Otto needs a macOS permission to read " + blockedSources.map { $0.0 }.joined(separator: ", ")
            } else {
                button.toolTip = itemCount > 0 ? "Briefings · \(itemCount) item\(itemCount == 1 ? "" : "s"), nothing urgent" : "Briefings · all clear — just keeping you up to date"
            }
        }
    }

    // MARK: click handling

    @objc func statusClicked(_ sender: Any?) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            showMenu()
            return
        }
        if BriefingsOpens.current == .browser {
            openBriefing()
            return
        }
        togglePanel()
    }

    func togglePanel() {
        if popover.isShown { closePanel(); return }
        guard let button = statusItem.button else { return }
        panel.refreshing = refreshing
        panel.offline = offline
        // Always open on the notifications; Worth knowing is a click away in the footer.
        panel.show(.briefings)
        if panel.payload == nil { panel.render(nil) }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        loadPanel()
        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.closePanel()
        }
    }

    func closePanel() {
        popover.performClose(nil)
        ImageViewerPanel.hide()
        if let m = outsideClickMonitor { NSEvent.removeMonitor(m); outsideClickMonitor = nil }
    }

    func popoverDidClose(_ notification: Notification) {
        if let m = outsideClickMonitor { NSEvent.removeMonitor(m); outsideClickMonitor = nil }
    }

    func loadPanel() {
        if offline { panel.render(nil); return }
        request("/api/items") { [weak self] json in
            guard let self = self else { return }
            guard let json = json else {
                self.offline = true
                self.panel.offline = true
                self.panel.render(nil)
                return
            }
            self.renderedUpdated = self.lastUpdated
            self.panel.refreshing = self.refreshing
            self.panel.render(Payload.parse(json))
        }
    }

    // MARK: menu

    func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        func add(_ title: String, _ action: Selector? = nil, key: String = "", enabled: Bool = true) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = self
            item.isEnabled = enabled && action != nil
            menu.addItem(item)
            return item
        }

        if offline {
            _ = add("Otto isn't running", nil)
            _ = add("Start Otto", #selector(startEngine), key: "s")
        } else {
            _ = add(itemCount > 0 ? "Briefings (\(itemCount))" : "Briefings", #selector(openBriefingsPreferred), key: "b")
            if importantCount > 0 {
                _ = add("   \(importantCount) need\(importantCount == 1 ? "s" : "") you", nil)
            } else if itemCount > 0 {
                _ = add("   \(itemCount) item\(itemCount == 1 ? "" : "s"), nothing urgent", nil)
            } else {
                _ = add("   All clear — just keeping you up to date", nil)
            }
            _ = add("   " + (refreshing ? "Refreshing…" : "Updated \(relativeAge())"), nil)
            if !lastError.isEmpty {
                _ = add("   Last refresh failed — details in otto.log", nil)
            }
            for (i, p) in problems.prefix(4).enumerated() {
                // The engine's "needs a look" list, each with its one action.
                let title = "⚠︎ " + p.title + (p.fixTitle.isEmpty ? "" : " — " + p.fixTitle)
                let item = add(title, p.fixTitle.isEmpty ? nil : #selector(fixProblem(_:)))
                item.tag = i
                item.toolTip = p.detail
            }
            menu.addItem(.separator())
            _ = add("Open in Browser", #selector(openBriefing), key: "o")
            _ = add("Refresh Now", #selector(refreshNow), key: "r", enabled: !refreshing)
            _ = add("Clear All", #selector(clearAll), key: "k", enabled: itemCount > 0)
        }

        // Settings and set-up: everything that used to need a terminal.
        menu.addItem(.separator())
        _ = add("Edit Config…", #selector(editConfig), key: ",")
        _ = add("Connect Slack…", #selector(connectSlack))
        _ = add("Add a Model Key…", #selector(addModelKey))
        if !blockedSources.isEmpty || problems.contains(where: { $0.action == "permissions" }) {
            _ = add("Fix macOS Permissions…", #selector(fixPermissions))
        }

        menu.addItem(.separator())
        let opens = NSMenuItem(title: "Briefings Open In", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        sub.autoenablesItems = false
        let inPanel = NSMenuItem(title: "This Panel", action: #selector(useBriefingsPanel), keyEquivalent: "")
        inPanel.target = self
        inPanel.state = BriefingsOpens.current == .panel ? .on : .off
        let inBrowser = NSMenuItem(title: "The Browser", action: #selector(useBriefingsBrowser), keyEquivalent: "")
        inBrowser.target = self
        inBrowser.state = BriefingsOpens.current == .browser ? .on : .off
        sub.addItem(inPanel)
        sub.addItem(inBrowser)
        opens.submenu = sub
        menu.addItem(opens)

        let login = add(loginItemInstalled() ? "Run at Login ✓" : "Run at Login", #selector(toggleLogin))
        login.state = loginItemInstalled() ? .on : .off
        if !offline {
            _ = add("Stop Otto", #selector(stopEngine))
        }
        menu.addItem(.separator())
        _ = add("Quit Otto Menu Bar", #selector(quitApp), key: "q")
        return menu
    }

    func showMenu() {
        closePanel()
        let menu = buildMenu()
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil            // back to click → panel
    }

    func panelWantsMenu(from view: NSView) {
        let menu = buildMenu()
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: view.bounds.height + 4), in: view)
    }

    // MARK: actions

    @objc func openBriefingsPreferred() {
        if BriefingsOpens.current == .browser { openBriefing() } else { togglePanel() }
    }

    @objc func openBriefing() {
        if let url = config.pageURL { NSWorkspace.shared.open(url) }
    }

    @objc func useBriefingsPanel() { BriefingsOpens.current = .panel }
    @objc func useBriefingsBrowser() { BriefingsOpens.current = .browser }

    func panelWantsRefresh() { refreshNow() }
    func panelWantsBrowser() { closePanel(); openBriefing() }
    func panelWantsClear(count: Int) { clearAll() }
    func panelWantsStart() { startEngine() }
    func panelWantsConfig() { editConfig() }
    func panelWantsFix(_ problem: Problem) { fix(problem) }
    func panelDidOpenSomething() { closePanel() }

    @objc func refreshNow() {
        refreshing = true
        panel.refreshing = true
        if popover.isShown { panel.render(panel.payload) }
        request("/api/refresh", method: "POST") { [weak self] _ in
            // Poll a few times until the engine reports the new data.
            var tries = 0
            Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { t in
                tries += 1
                self?.checkStatus()
                if tries >= 20 || !(self?.refreshing ?? false) { t.invalidate() }
            }
        }
    }

    /// One click, same as the page: the briefing empties at once, the items
    /// stay in Otto's memory (and come back on their own if they are still
    /// live and important), radar entries are kept. No dialog — a clear you
    /// have to confirm is a clear you stop using.
    @objc func clearAll() {
        itemCount = 0; importantCount = 0
        updateStatusButton()
        if popover.isShown { panel.showCleared() }
        request("/api/clear", method: "POST") { [weak self] _ in
            self?.checkStatus()
            if self?.popover.isShown == true { self?.loadPanel() }
        }
    }

    /// Run `otto <args>` with nothing on stdin and nothing kept from stdout.
    /// The port is not pinned here: the CLI finds the running engine itself
    /// (`engine.json`), so a port changed in config.toml needs only a restart.
    private func runOtto(_ args: [String], completion: (() -> Void)? = nil) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: config.python)
        p.arguments = ["-m", "otto.cli"] + args
        p.environment = OttoTask.environment(config)
        p.currentDirectoryURL = URL(fileURLWithPath: config.home)
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in DispatchQueue.main.async { completion?() } }
        try? p.run()
    }

    @objc func startEngine() {
        runOtto(["start"]) { [weak self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self?.checkStatus() }
        }
    }

    @objc func stopEngine() {
        runOtto(["stop"]) { [weak self] in self?.checkStatus() }
    }

    /// Opens System Settings → Accessibility and reveals the engine's interpreter in Finder
    /// (via `otto permissions`), then re-checks once the engine has retried.
    @objc func fixPermissions() {
        runOtto(["permissions"]) { [weak self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.checkStatus() }
        }
    }

    // MARK: settings and set-up (what used to need a terminal)

    /// Edit Config…: config.toml (every setting explained, keys under [keys])
    /// in Otto's own editor window — Save validates and the engine reloads
    /// right away. "Open in Editor" there hands the file to the default text
    /// editor instead (`otto config`).
    @objc func editConfig() {
        closePanel()
        if configEditor == nil {
            let editor = ConfigEditor(config: config)
            editor.onSaved = { [weak self] in
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self?.checkStatus() }
            }
            editor.openExternally = { [weak self] in
                self?.runOtto(["config"]) { [weak self] in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self?.checkStatus() }
                }
            }
            configEditor = editor
        }
        configEditor?.show()
    }

    @objc func fixProblem(_ sender: NSMenuItem) {
        guard sender.tag >= 0, sender.tag < problems.count else { return }
        fix(problems[sender.tag])
    }

    func fix(_ problem: Problem) {
        switch problem.action {
        case "permissions": fixPermissions()
        case "slack": connectSlack()
        case "config": editConfig()
        case "restart": runOtto(["restart"]) { [weak self] in self?.checkStatus() }
        case _ where problem.action.hasPrefix("key_remove:"):
            let prefix = String(problem.action.dropFirst("key_remove:".count))
            guard !prefix.isEmpty else { return }
            runOtto(["key", "remove", prefix]) { [weak self] in
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.checkStatus() }
            }
        default: break
        }
    }

    /// Connect Slack… — the same flow as `otto slack connect`, without the
    /// terminal: Slack's create-app page opens with Otto's read-only manifest
    /// filled in; the token is pasted into a secure field here and handed to
    /// the CLI on stdin (never an argument, never a URL, never the network);
    /// the CLI verifies it with Slack, stores it 0600 and restarts the engine.
    @objc func connectSlack() {
        guard runningTask == nil else { return }
        closePanel()
        let task = OttoTask(config: config, args: ["slack", "connect", "--json"])
        runningTask = task
        var opened = false
        task.onLine = { [weak self] line in
            guard let self = self else { return }
            let step = (line["step"] as? String) ?? ""
            if step == "url", !opened {
                opened = true
                let url = (line["url"] as? String) ?? ""
                // Only Slack's own create-app page, only that fixed link.
                if url.hasPrefix("https://api.slack.com/apps?"), let u = URL(string: url) { NSWorkspace.shared.open(u) }
                let token = self.askSecret(
                    title: "Connect Slack (read-only)",
                    text: "Slack's “create an app” page is opening in your browser with Otto's manifest already filled in.\n\n"
                        + "1. Sign in if asked, pick your workspace, click Create\n"
                        + "2. Install to Workspace → Allow\n"
                        + "3. OAuth & Permissions → copy the User OAuth Token (starts with xoxp-)\n\n"
                        + "Paste it here. Otto can only read with it — never post, react, edit or join.",
                    placeholder: "xoxp-…", button: "Connect")
                task.send(token ?? "")           // empty → the CLI answers "cancelled"
            } else if step == "done" {
                let ok = (line["ok"] as? Bool) ?? false
                if ok {
                    let who = (line["identity"] as? String) ?? "verified"
                    let coverage = (line["coverage"] as? String) ?? ""
                    let warnings = (line["warnings"] as? [String]) ?? []
                    var text = "Connected as \(who)."
                    if !coverage.isEmpty { text += "\nOtto now reads: \(coverage)." }
                    if !warnings.isEmpty { text += "\n\n" + warnings.joined(separator: "\n") }
                    text += "\n\nGive it a minute; the badge and the panel show what it finds."
                    self.inform(title: "Slack connected", text: text)
                } else {
                    let error = (line["error"] as? String) ?? "not stored"
                    if error != "cancelled" {
                        self.inform(title: "Slack was not connected", text: error + "\n\nNothing was stored. Try again from the menu whenever you like.")
                    }
                }
            }
        }
        task.onExit = { [weak self] _ in
            self?.runningTask = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.checkStatus() }
        }
        if !task.start() { runningTask = nil }
    }

    /// Add a Model Key… — `otto key add --json`: the key goes to the CLI on
    /// stdin, is stored 0600 outside the repo, and the engine is restarted so
    /// the next refresh uses it. Only key shapes Otto recognises are accepted.
    @objc func addModelKey() {
        guard runningTask == nil else { return }
        closePanel()
        guard let key = askSecret(
            title: "Add a model key",
            text: "Otto works without one (local signals only). A key from OpenRouter, OpenAI, Anthropic, Gemini or Devin "
                + "adds written summaries and better ordering.\n\nThe key is stored in a 0600 file under ~/Library/Application Support/Otto "
                + "— never in the project, never printed in full. (Edit Config… → [keys] works too.)",
            placeholder: "sk-…", button: "Add"), !key.isEmpty else { return }
        let task = OttoTask(config: config, args: ["key", "add", "--json"])
        runningTask = task
        task.onLine = { [weak self] line in
            let ok = (line["ok"] as? Bool) ?? false
            if ok {
                let masked = (line["masked"] as? String) ?? "key"
                let kind = (line["kind"] as? String) ?? ""
                let restarted = (line["restarted"] as? Bool) ?? false
                let where_ = (line["where"] as? String) ?? ""
                var text = "\(masked)" + (kind.isEmpty ? "" : "  (\(kind))")
                text += where_ == "already stored" ? "\n\nWas already stored — nothing changed." : "\n\nStored."
                text += restarted ? " Otto restarted and uses it from the next refresh." : " Start Otto to use it."
                self?.inform(title: "Model key added", text: text)
            } else {
                self?.inform(title: "Key not added", text: (line["error"] as? String) ?? "not stored")
            }
        }
        task.onExit = { [weak self] _ in
            self?.runningTask = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.checkStatus() }
        }
        if task.start() { task.send(key) } else { runningTask = nil }
    }

    /// A modal with a secure text field. Returns nil on Cancel.
    private func askSecret(title: String, text: String, placeholder: String, button: String) -> String? {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .informational
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 24))
        field.placeholderString = placeholder
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        return field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func inform(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    private var launchAgentPath: String {
        let dir = ProcessInfo.processInfo.environment["OTTO_LAUNCH_AGENTS_DIR"] ?? (NSHomeDirectory() + "/Library/LaunchAgents")
        return dir + "/com.otto.engine.plist"
    }

    private func loginItemInstalled() -> Bool {
        FileManager.default.fileExists(atPath: launchAgentPath)
    }

    /// "Run at Login" is about the engine. Turning it off must not quit the
    /// very menu the user is clicking in, hence --keep-menubar.
    @objc func toggleLogin() {
        runOtto(loginItemInstalled() ? ["uninstall", "--keep-menubar"] : ["install"]) { [weak self] in
            self?.checkStatus()
        }
    }

    @objc func quitApp() { NSApplication.shared.terminate(self) }

    // MARK: notifications

    /// A banner for one item; when the engine has a thumbnail of the source (the
    /// Slack window showing that conversation) it rides along as the attachment,
    /// so the notification itself shows where the news came from.
    func showBanner(id: String, title: String, body: String, url: String, imagePath: String = "") {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.userInfo = ["url": url]
        if let attachment = Self.attachment(forImageAt: imagePath, id: id) {
            content.attachments = [attachment]
        }
        let req = UNNotificationRequest(identifier: "otto-\(id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    /// UNNotificationAttachment takes ownership of (moves) the file it is given,
    /// so attach a private copy and leave the engine's thumbnail where it is.
    static func attachment(forImageAt path: String, id: String) -> UNNotificationAttachment? {
        guard !path.isEmpty, FileManager.default.isReadableFile(atPath: path) else { return nil }
        let copy = FileManager.default.temporaryDirectory
            .appendingPathComponent("otto-banner-\(id)-\(UUID().uuidString).png")
        do {
            try FileManager.default.copyItem(at: URL(fileURLWithPath: path), to: copy)
            return try UNNotificationAttachment(identifier: "source", url: copy, options: nil)
        } catch {
            try? FileManager.default.removeItem(at: copy)
            return nil
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let urlString = response.notification.request.content.userInfo["url"] as? String ?? ""
        DispatchQueue.main.async {
            if !openExternal(urlString) {
                self.openBriefingsPreferred()
            }
        }
        completionHandler()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: NSUserNotificationCenter, didActivate notification: NSUserNotification) {
        DispatchQueue.main.async {
            self.openBriefingsPreferred()
        }
    }

    func userNotificationCenter(_ center: NSUserNotificationCenter, shouldPresent notification: NSUserNotification) -> Bool {
        return true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openBriefingsPreferred()
        return true
    }
}

// MARK: - Entry

let args = CommandLine.arguments

// `--notify title body [image]`: the engine's fallback banner when no menu bar app is listening.
if args.contains("--notify") {
    if let idx = args.firstIndex(of: "--notify"), idx + 2 < args.count {
        let title = args[idx + 1]
        let body = args[idx + 2]
        let n = NSUserNotification()
        n.title = title
        n.informativeText = body
        n.soundName = NSUserNotificationDefaultSoundName
        if idx + 3 < args.count, let image = NSImage(contentsOfFile: args[idx + 3]) {
            n.contentImage = image          // the source thumbnail, same as the menu bar banner
        }
        NSUserNotificationCenter.default.deliver(n)
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.25))
    }
    exit(0)
}

// `--preview items.json out.png [--dark] [--dismiss-first] [--worth-knowing [--clear]]`:
// draw the panel for a saved /api/items payload into a PNG without showing
// anything — how the panel is checked without a person clicking the menu bar.
// `--worth-knowing` opens the Worth knowing view (as the footer button would);
// with `--clear` its Clear is pressed first (the engine call goes nowhere here).
if let idx = args.firstIndex(of: "--preview"), idx + 2 < args.count {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    guard let data = FileManager.default.contents(atPath: args[idx + 1]),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        FileHandle.standardError.write("preview: could not read \(args[idx + 1])\n".data(using: .utf8)!)
        exit(2)
    }
    let controller = PanelController()
    // /api/status's "problems" may ride along in the saved payload to preview the rows.
    controller.problems = Problem.parse((json["problems"] as? [[String: Any]]) ?? [])
    // Same order as the first click on the icon: togglePanel renders the empty
    // panel before the popover has loaded the view, then the payload arrives.
    controller.render(nil)
    controller.render(Payload.parse(json))
    if args.contains("--worth-knowing") {
        controller.show(.worthKnowing)
        if args.contains("--clear") { controller.rehearseClearWorthKnowing() }
    }
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: Look.width, height: 600),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: args.contains("--dark") ? .darkAqua : .aqua)
    window.contentViewController = controller
    // The popover supplies its own material; the bitmap needs a floor to read against.
    controller.view.wantsLayer = true
    controller.view.layer?.backgroundColor = (args.contains("--dark") ? NSColor(white: 0.13, alpha: 1) : NSColor.white).cgColor
    controller.fit()
    window.setContentSize(controller.preferredContentSize)
    window.layoutIfNeeded()
    // `--dismiss-first`: dismiss a row and rebuild the list while it fades —
    // the picture must come out without that row, and the process must live.
    if args.contains("--dismiss-first") {
        guard controller.rehearseDismiss() else {
            FileHandle.standardError.write("preview: no item row to dismiss\n".data(using: .utf8)!)
            exit(2)
        }
    }
    // Thumbnails load asynchronously; give them a moment.
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.6))
    let view = controller.view
    view.layoutSubtreeIfNeeded()
    // `cacheDisplay` does not draw the scroll bar itself (window chrome), so the
    // bar's state is reported in words next to the picture: whether it is the
    // always-visible legacy kind, its width and the knob's share of the track.
    let scroller = controller.scrollerReport
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(3) }
    view.cacheDisplay(in: view.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(3) }
    do {
        try png.write(to: URL(fileURLWithPath: args[idx + 2]))
        print("preview: \(Int(view.bounds.width))×\(Int(view.bounds.height)) → \(args[idx + 2])  [\(scroller)]")
    } catch {
        FileHandle.standardError.write("preview: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
    exit(0)
}

// `--preview-config file.toml out.png [--dark] [--error text] [--unknown a,b]`:
// draw the Edit Config… window for a given file into a PNG without showing
// anything and without the CLI — nothing is read from the data dir or written.
if let idx = args.firstIndex(of: "--preview-config"), idx + 2 < args.count {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    guard let text = try? String(contentsOfFile: args[idx + 1], encoding: .utf8) else {
        FileHandle.standardError.write("preview-config: could not read \(args[idx + 1])\n".data(using: .utf8)!)
        exit(2)
    }
    func flag(_ name: String) -> String? {
        guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
        return args[i + 1]
    }
    let editor = ConfigEditor(config: OttoConfig.load())
    let view = editor.previewView(text: text, path: args[idx + 1], error: flag("--error") ?? "",
                                  unknown: (flag("--unknown") ?? "").split(separator: ",").map(String.init))
    view.window?.appearance = NSAppearance(named: args.contains("--dark") ? .darkAqua : .aqua)
    // The window paints its own backdrop on screen; the bitmap needs one too.
    view.wantsLayer = true
    view.layer?.backgroundColor = (args.contains("--dark") ? NSColor(white: 0.13, alpha: 1) : NSColor(white: 0.93, alpha: 1)).cgColor
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.3))
    view.layoutSubtreeIfNeeded()
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(3) }
    view.cacheDisplay(in: view.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(3) }
    do {
        try png.write(to: URL(fileURLWithPath: args[idx + 2]))
        print("preview-config: \(Int(view.bounds.width))×\(Int(view.bounds.height)) → \(args[idx + 2])")
    } catch {
        FileHandle.standardError.write("preview-config: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
    exit(0)
}

// `--icon out.png`: the menu bar glyph in each of its states (clear, 1, 12,
// 99+, permission needed, offline) on a light and a dark bar, at 2×.
if let idx = args.firstIndex(of: "--icon"), idx + 1 < args.count {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let states: [(Int, Bool, Bool)] = [(0, false, false), (1, false, false), (12, false, false),
                                       (120, false, false), (0, true, false), (0, false, true)]
    let barHeight: CGFloat = 24, cell: CGFloat = 44
    let size = NSSize(width: cell * CGFloat(states.count), height: barHeight * 2)
    let scale: CGFloat = 2
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let ctx = NSGraphicsContext(bitmapImageRep: rep) else { exit(3) }
    rep.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    ctx.cgContext.scaleBy(x: scale, y: scale)
    for (row, dark) in [false, true].enumerated() {
        let y = CGFloat(row) * barHeight
        (dark ? NSColor(white: 0.16, alpha: 1) : NSColor(white: 0.93, alpha: 1)).setFill()
        NSRect(x: 0, y: y, width: size.width, height: barHeight).fill()
        for (i, s) in states.enumerated() {
            let img = StatusIcon.image(count: s.0, alert: s.1)
            let tint = (dark ? NSColor.white : NSColor.black).withAlphaComponent(s.2 ? 0.35 : 1)   // offline → appearsDisabled
            let frame = NSRect(x: CGFloat(i) * cell + (cell - img.size.width) / 2, y: y + (barHeight - img.size.height) / 2,
                               width: img.size.width, height: img.size.height)
            // Tint on a transparent canvas first (what the bar does with a template image).
            let tinted = NSImage(size: img.size, flipped: false) { r in
                img.draw(in: r)
                tint.setFill()
                r.fill(using: .sourceAtop)
                return true
            }
            tinted.draw(in: frame)
        }
    }
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(3) }
    do {
        try png.write(to: URL(fileURLWithPath: args[idx + 1]))
        print("icon: \(states.count) states × light/dark → \(args[idx + 1])")
    } catch {
        FileHandle.standardError.write("icon: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
    exit(0)
}

// One icon only: a second copy (e.g. `open bin/Otto.app` while the login
// agent already runs one) quits quietly instead of doubling the menu bar.
if let bundleId = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).count > 1 {
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
