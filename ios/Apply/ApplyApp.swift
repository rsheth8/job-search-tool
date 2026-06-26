import SwiftUI

@main
struct ApplyApp: App {
    @StateObject private var config = Config.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(config)
        }
    }
}

/// Tabbed shell: your queue of matches, and settings.
struct RootView: View {
    var body: some View {
        TabView {
            QueueView()
                .tabItem { Label("Apply", systemImage: "paperplane.fill") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}
