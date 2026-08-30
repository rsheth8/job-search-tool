import Foundation

/// Sample quiz payload so you can walk every screen without saving anything.
enum QuizDemo {
    static let roles = "New grad SWE, backend intern"
    static let locations = "NYC, remote, Chicago"
    static let keywords = "Python, React, SQL"
    static let seniority = "New grad"
    static let project = "Built a matcher that ranks Greenhouse roles against a résumé."
    static let achievement = "Cut the time to send a tailored application from 40 minutes to 4."
    static let strength = "I like the unglamorous glue that makes a product feel finished."
    static let preference = "Remote or NYC. New-grad software roles, not data-entry."
    static let about = "I’m a new grad who likes backend systems and calm product surfaces."
    static let whyRole = "I want to work on tools people open every week, not once a year."

    static var identity: IdentityDraft {
        var id = IdentityDraft()
        id.firstName = "Ada"
        id.lastName = "Chen"
        id.preferredName = "Ada"
        id.pronouns = "she/her"
        id.email = "ada@school.edu"
        id.phone = "555-0100"
        id.address = "1100 E 58th St"
        id.city = "Chicago"
        id.state = "IL"
        id.zip = "60637"
        id.country = "United States"
        id.linkedin = "https://linkedin.com/in/adachen"
        id.github = "adachen"
        id.portfolio = "https://ada.example"
        id.school = "University of Chicago"
        id.degree = "BS"
        id.discipline = "Computer Science"
        id.gpa = "3.8"
        id.gradYear = "2026"
        id.currentCompany = "Campus lab"
        id.currentTitle = "Software intern"
        id.years = "1"
        id.salary = "120000"
        id.startDate = "Summer 2026"
        id.workArrangement = "Remote or hybrid"
        id.howHeard = "Career site"
        id.gender = "Woman"
        id.race = "Asian"
        id.ethnicity = "Not Hispanic or Latino"
        id.veteranStatus = "I am not a veteran"
        id.disabilityStatus = "No"
        id.workAuthorized = true
        id.needsSponsorship = false
        id.willingToRelocate = true
        id.backgroundCheck = true
        id.drugTest = true
        id.over18 = true
        id.canTravel = false
        id.previouslyApplied = false
        id.relatedToEmployee = false
        return id
    }

    static let missing: [String] = ["salary_expectation", "how_heard"]

    static func importResult(source: String) -> ImportResult {
        let filled: [String]
        switch source {
        case "github":
            filled = ["github", "first_name", "portfolio"]
        case "linkedin":
            filled = ["linkedin", "first_name", "last_name", "current_title"]
        default:
            filled = [
                "first_name", "last_name", "email", "phone", "city", "state",
                "school", "degree", "discipline", "github", "linkedin",
            ]
        }
        return ImportResult(
            ok: true,
            source: source,
            filled: filled,
            knowledge_added: source == "resume" ? 3 : 1,
            identity_score: source == "resume" ? 0.74 : 0.58,
            note: "Preview only — nothing was saved.",
            draft: draft
        )
    }

    static var draft: QuizDraft {
        QuizDraft(
            project: project,
            achievement: achievement,
            strength: strength,
            preference: preference,
            about: about,
            why_role: whyRole,
            roles: roles,
            locations: locations,
            keywords: keywords,
            seniority: seniority
        )
    }

    static func score(forStepIndex index: Int, total: Int) -> Double {
        let last = max(total - 1, 1)
        return 0.22 + (Double(index) / Double(last)) * 0.70
    }
}
