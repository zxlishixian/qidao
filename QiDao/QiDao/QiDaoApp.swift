//
//  QiDaoApp.swift
//  QiDao
//
//  Created by Neo on 2025/12/24.
//

import SwiftUI

@main
struct QiDaoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @ObservedObject private var langManager = LanguageManager.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .defaultSize(width: 1180, height: 760)
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) { }
            CommandGroup(replacing: .undoRedo) { }
            CommandGroup(replacing: .printItem) { }
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    private var configuredWindows = Set<ObjectIdentifier>()
    private var mainWindowObserver: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        mainWindowObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeMainNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let window = notification.object as? NSWindow else { return }
            self?.configureMainWindowIfNeeded(window)
        }

        // SwiftUI may restore its saved window before or just after the app
        // finishes launching. Check both the existing list and the main-window
        // notification so an oversized historical frame cannot escape repair.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            NSApp.windows.forEach { self?.configureMainWindowIfNeeded($0) }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let mainWindowObserver {
            NotificationCenter.default.removeObserver(mainWindowObserver)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    private func configureMainWindowIfNeeded(_ window: NSWindow) {
        guard !(window is NSPanel), window.styleMask.contains(.titled) else { return }
        let windowIdentifier = window.identifier?.rawValue ?? ""
        guard windowIdentifier.contains("ContentView") || window.title == "QiDao 棋道" else {
            return
        }
        let identifier = ObjectIdentifier(window)
        guard configuredWindows.insert(identifier).inserted else { return }
        guard let screen = window.screen ?? NSScreen.main else { return }

        let visible = screen.visibleFrame.insetBy(dx: 8, dy: 8)
        window.contentMinSize = NSSize(
            width: min(900, visible.width),
            height: min(600, visible.height)
        )

        var frame = window.frame
        let oversized = frame.width > visible.width || frame.height > visible.height
        if oversized {
            frame.size = NSSize(
                width: min(1180, visible.width),
                height: min(760, visible.height)
            )
            frame.origin = NSPoint(
                x: visible.midX - frame.width / 2,
                y: visible.midY - frame.height / 2
            )
        } else {
            // Preserve a valid user size while bringing off-screen edges back
            // into the current display's usable area.
            frame.origin.x = min(
                max(frame.origin.x, visible.minX),
                visible.maxX - frame.width
            )
            frame.origin.y = min(
                max(frame.origin.y, visible.minY),
                visible.maxY - frame.height
            )
        }

        if frame != window.frame {
            window.setFrame(frame, display: true, animate: false)
        }
    }
}
