import Foundation
import SwiftUI

/// Whether the signed-in user still needs the first-run wizard.
@MainActor
final class SetupGate: ObservableObject {
    static let shared = SetupGate()

    @Published var needsSetup = false
    @Published var status: SetupStatus?
    @Published var loaded = false

    func refresh(config: Config) async {
        guard config.isSignedIn else {
            needsSetup = false
            loaded = true
            return
        }
        do {
            let s = try await APIClient(config: config).fetchSetup()
            status = s
            needsSetup = s.needs_setup
            loaded = true
        } catch {
            if APIClient.isCancellation(error) { return }
            // Don't trap a signed-in user on the wizard because of a blip.
            loaded = true
        }
    }

    func reopen() {
        needsSetup = true
    }
}
