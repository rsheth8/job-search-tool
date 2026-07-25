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

/// Tabbed shell: matches to apply to, what the worker is doing, what it knows about
/// you, and settings.
struct RootView: View {
    var body: some View {
        TabView {
            QueueView()
                .tabItem { Label("Apply", systemImage: "paperplane.fill") }
            InFlightView()
                .tabItem { Label("In flight", systemImage: "arrow.triangle.2.circlepath") }
            KnowledgeView()
                .tabItem { Label("About me", systemImage: "person.text.rectangle") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}
