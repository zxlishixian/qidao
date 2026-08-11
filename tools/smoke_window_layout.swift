import AppKit
import SwiftUI

@main
struct WindowLayoutSmoke {
    @MainActor
    static func main() {
        let hosting = NSHostingView(rootView: ContentView())
        let window = NSWindow(
            contentRect: NSRect(x: -2200, y: -2200, width: 1180, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.contentMinSize = NSSize(width: 900, height: 600)
        window.contentView = hosting
        window.orderFrontRegardless()
        window.setContentSize(NSSize(width: 900, height: 600))
        window.contentView?.layoutSubtreeIfNeeded()
        window.displayIfNeeded()

        let contentSize = window.contentLayoutRect.size
        guard abs(contentSize.width - 900) < 1,
              abs(contentSize.height - 600) < 1 else {
            fatalError(
                "Main content refused 900×600: "
                    + "\(Int(contentSize.width))×\(Int(contentSize.height))"
            )
        }

        let scrollViewCount = countScrollViews(in: hosting)
        guard scrollViewCount >= 2 else {
            fatalError("Expected independent sidebar scrollers, found \(scrollViewCount)")
        }

        window.orderOut(nil)
        print(
            "Window layout OK: content \(Int(contentSize.width))×\(Int(contentSize.height)), "
                + "scroll views \(scrollViewCount)"
        )
    }

    @MainActor
    private static func countScrollViews(in view: NSView) -> Int {
        (view is NSScrollView ? 1 : 0)
            + view.subviews.reduce(0) { $0 + countScrollViews(in: $1) }
    }
}
