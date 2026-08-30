#if canImport(FoundationModels)
import Foundation
import FoundationModels

/// Guided generation against the on-device system language model.
@available(iOS 26, *)
enum OnDeviceSession {
    static var isAvailable: Bool {
        SystemLanguageModel.default.isAvailable
    }

    static func classify(_ text: String) async throws -> AgentTurnPayload {
        let session = LanguageModelSession {
            """
            You are Horizon, JobPilot's in-app copilot.
            Classify the user message into one action. Do not invent jobs.
            Never claim you submitted an application. You only classify.

            Actions:
            - helpApp: how to use the app (autofill, submit, résumé attach, identity, import, notifications). Put the topic in topic (autofill, submit, resume, identity, queue, jobs, filed, search, add, import, notifications, feedback, quiz, overview).
            - openTab: user wants to go to a screen. Put the dest in tab: apply, apply:filed, you, you:identity, you:search, you:add, you:projects, you:import, settings, settings:notifications, settings:feedback, settings:quiz, setup, or chat.
            - setIdentity: change form details (phone, email, location, linkedin, github, name, work auth, sponsorship). Put the field key in field and the new value in value.
            - profile: set or show job-search criteria (roles/locations). Put criteria in message.
            - jobs: browse discovered matches.
            - jobsReview: walk through matches one by one.
            - queueJob: stage a posting. Put job id in jobId, or count for "queue top N".
            - applyJob: apply to a surfaced posting by jobId or company.
            - dismissJob / snoozeJob: hide or mute a posting (jobId).
            - tune: change match pickiness. Put loosen, tighten, all, reset, or set:0.8 in message.
            - track: watch a company board. company = name; message = list or remove if needed.
            - remember: store a fact. category + text, or message.
            - knowledge: what's known / missing about the user.
            - list, query, stats, deadline, check, apply, update, note, remind, edit, delete, bulk, undo: pipeline CRM (same as the old commands).
            - unknown: none of the above.

            Fill only the slots you are sure about. Leave the rest empty.
            """
        }
        let result = try await session.respond(to: text, generating: OnDeviceTurn.self)
        return result.content.payload
    }
}

@available(iOS 26, *)
@Generable
struct OnDeviceTurn {
    var action: OnDeviceAction
    var topic: String?
    var tab: String?
    var field: String?
    var value: String?
    var company: String?
    var role: String?
    var status: String?
    var message: String?
    var timeReference: String?
    var jobId: Int?
    var count: Int?
}

@available(iOS 26, *)
@Generable
enum OnDeviceAction {
    case helpApp
    case openTab
    case setIdentity
    case profile
    case jobs
    case jobsReview
    case queueJob
    case applyJob
    case dismissJob
    case snoozeJob
    case tune
    case track
    case remember
    case knowledge
    case list
    case query
    case stats
    case deadline
    case check
    case apply
    case update
    case note
    case remind
    case edit
    case delete
    case bulk
    case undo
    case unknown
}

@available(iOS 26, *)
extension OnDeviceTurn {
    var payload: AgentTurnPayload {
        var slots: [String: Any] = [:]
        if let topic, !topic.isEmpty { slots["topic"] = topic }
        if let tab, !tab.isEmpty { slots["tab"] = tab }
        if let field, !field.isEmpty { slots["field"] = field }
        if let value, !value.isEmpty { slots["value"] = value }
        if let company, !company.isEmpty { slots["company"] = company }
        if let role, !role.isEmpty { slots["role"] = role }
        if let status, !status.isEmpty { slots["status"] = status }
        if let message, !message.isEmpty { slots["message"] = message }
        if let timeReference, !timeReference.isEmpty { slots["time_reference"] = timeReference }
        if let jobId { slots["job_id"] = jobId }
        if let count { slots["count"] = count }

        let action: String
        switch self.action {
        case .helpApp: action = "HELP_APP"
        case .openTab:
            action = "HELP_APP"
            if slots["tab"] == nil, let topic {
                slots["tab"] = topic
            }
        case .setIdentity: action = "SET_IDENTITY"
        case .profile: action = "PROFILE"
        case .jobs: action = "JOBS"
        case .jobsReview: action = "JOBS_REVIEW"
        case .queueJob: action = "QUEUE_JOB"
        case .applyJob: action = "APPLY_JOB"
        case .dismissJob: action = "DISMISS_JOB"
        case .snoozeJob: action = "SNOOZE_JOB"
        case .tune: action = "TUNE"
        case .track: action = "TRACK"
        case .remember: action = "REMEMBER"
        case .knowledge: action = "KNOWLEDGE"
        case .list: action = "LIST"
        case .query: action = "QUERY"
        case .stats: action = "STATS"
        case .deadline: action = "DEADLINE"
        case .check: action = "CHECK"
        case .apply: action = "APPLY"
        case .update: action = "UPDATE"
        case .note: action = "NOTE"
        case .remind: action = "REMIND"
        case .edit: action = "EDIT"
        case .delete: action = "DELETE"
        case .bulk: action = "BULK"
        case .undo: action = "UNDO"
        case .unknown: action = "UNKNOWN"
        }
        return AgentTurnPayload(action: action, slots: slots, confidence: 0.9)
    }
}
#endif
