import SwiftUI

@main
struct ApplyApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var config = Config.shared
    @StateObject private var push = PushManager.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(config)
                .environmentObject(push)
                .task { await push.refreshAuthorization() }
        }
    }
}

/// Tabbed shell: matches to apply to, what the worker is doing, what it knows about
/// you, and settings.
struct RootView: View {
    @EnvironmentObject var push: PushManager

    /// Bound to PushManager so a tapped notification lands on the tab that answers
    /// it — an approval request opens In flight, new matches open Apply.
    var body: some View {
        TabView(selection: $push.selectedTab) {
            QueueView()
                .tabItem { Label("Apply", systemImage: "paperplane.fill") }
                .tag(0)
            InFlightView()
                .tabItem { Label("In flight", systemImage: "arrow.triangle.2.circlepath") }
                .tag(1)
            KnowledgeView()
                .tabItem { Label("About me", systemImage: "person.text.rectangle") }
                .tag(2)
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(3)
        }
        // Set once on the shell rather than per tab: the accent has to reach the tab
        // bar itself, and a tint applied inside a tab stops at that tab's content.
        // Light/dark needs no switch here — Theme's colours resolve per trait
        // collection, so the system setting is the toggle.
        .tint(Theme.accentColor)
    }
}
