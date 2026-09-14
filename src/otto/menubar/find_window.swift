// find_window — print the CGWindowID of the first on-screen window owned by
// the named application (default: Slack). Used by Otto to take a screenshot
// of the Slack window with `screencapture -l<id>` without focusing it.
//
// Usage: find_window [AppName]      exit 0 + window id on stdout, exit 1 if none

import CoreGraphics
import Foundation

let target = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Slack"
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let list = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []

for w in list {
    guard let owner = w[kCGWindowOwnerName as String] as? String,
          owner.caseInsensitiveCompare(target) == .orderedSame,
          let bounds = w[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? Double, width > 200,
          let id = w[kCGWindowNumber as String] as? Int else { continue }
    print(id)
    exit(0)
}
exit(1)
