import Foundation

/// Applicant identity the quiz and the You-tab editor both read/write.
/// Keys match ``app/applicant.py`` so Autofill can fill the live form.
struct IdentityDraft {
    var firstName = ""
    var lastName = ""
    var preferredName = ""
    var pronouns = ""
    var email = ""
    var phone = ""
    var address = ""
    var city = ""
    var state = ""
    var zip = ""
    var country = ""
    var linkedin = ""
    var github = ""
    var portfolio = ""
    /// Every degree, most relevant first. When this has entries the server
    /// derives `school`/`degree`/`gpa`/… from it, so those flat properties are
    /// a read-through view rather than the source of truth.
    var education: [EducationEntry] = []
    var school = ""
    var degree = ""
    var discipline = ""
    var gpa = ""
    var gradYear = ""
    var gradMonth = ""
    var currentCompany = ""
    var currentTitle = ""
    var years = ""
    var salary = ""
    var startDate = ""
    var internSeason = ""
    var workArrangement = ""
    var howHeard = ""
    var gender = ""
    var race = ""
    var ethnicity = ""
    var veteranStatus = ""
    var disabilityStatus = ""
    var workAuthorized = true
    var needsSponsorship = false
    var willingToRelocate = false
    var backgroundCheck = true
    var drugTest = true
    var over18 = true
    var canTravel = false
    var previouslyApplied = false
    var relatedToEmployee = false

    mutating func load(from id: [String: String]) {
        if firstName.isEmpty { firstName = id["first_name"] ?? "" }
        if lastName.isEmpty { lastName = id["last_name"] ?? "" }
        if preferredName.isEmpty { preferredName = id["preferred_name"] ?? "" }
        if pronouns.isEmpty { pronouns = id["pronouns"] ?? "" }
        if email.isEmpty { email = id["email"] ?? "" }
        if phone.isEmpty { phone = id["phone"] ?? "" }
        if address.isEmpty { address = id["address"] ?? "" }
        if city.isEmpty { city = id["city"] ?? "" }
        if state.isEmpty { state = id["state"] ?? "" }
        if zip.isEmpty { zip = id["zip"] ?? "" }
        if country.isEmpty { country = id["country"] ?? "" }
        if linkedin.isEmpty { linkedin = id["linkedin"] ?? "" }
        if github.isEmpty { github = id["github"] ?? "" }
        if portfolio.isEmpty { portfolio = id["portfolio"] ?? "" }
        if school.isEmpty { school = id["school"] ?? "" }
        if degree.isEmpty { degree = id["degree"] ?? "" }
        if discipline.isEmpty { discipline = id["discipline"] ?? "" }
        if gpa.isEmpty { gpa = id["gpa"] ?? "" }
        if gradYear.isEmpty { gradYear = id["grad_year"] ?? "" }
        if gradMonth.isEmpty { gradMonth = id["grad_month"] ?? "" }
        if currentCompany.isEmpty { currentCompany = id["current_company"] ?? "" }
        if currentTitle.isEmpty { currentTitle = id["current_title"] ?? "" }
        if years.isEmpty { years = id["years_experience"] ?? "" }
        if salary.isEmpty { salary = id["salary_expectation"] ?? "" }
        if startDate.isEmpty { startDate = id["start_date"] ?? "" }
        if internSeason.isEmpty { internSeason = id["intern_season"] ?? "" }
        if workArrangement.isEmpty { workArrangement = id["work_arrangement"] ?? "" }
        if howHeard.isEmpty { howHeard = id["how_heard"] ?? "" }
        if gender.isEmpty { gender = id["gender"] ?? "" }
        if race.isEmpty { race = id["race"] ?? "" }
        if ethnicity.isEmpty { ethnicity = id["ethnicity"] ?? "" }
        if veteranStatus.isEmpty { veteranStatus = id["veteran_status"] ?? "" }
        if disabilityStatus.isEmpty { disabilityStatus = id["disability_status"] ?? "" }
        if let v = id["work_authorized"] { workAuthorized = v != "false" }
        if let v = id["needs_sponsorship"] { needsSponsorship = v == "true" }
        if let v = id["willing_to_relocate"] { willingToRelocate = v == "true" }
        if let v = id["background_check"] { backgroundCheck = v != "false" }
        if let v = id["drug_test"] { drugTest = v != "false" }
        if let v = id["over_18"] { over18 = v != "false" }
        if let v = id["can_travel"] { canTravel = v == "true" }
        if let v = id["previously_applied"] { previouslyApplied = v == "true" }
        if let v = id["related_to_employee"] { relatedToEmployee = v == "true" }
    }

    /// Only the given keys. Empty strings are omitted by default so a quiz skip
    /// never wipes a saved value. Pass ``omitEmpty: false`` from the editor so
    /// clearing a field actually clears it.
    func payload(keys: Set<String>, omitEmpty: Bool = true) -> [String: Any] {
        let all: [String: Any] = [
            "first_name": firstName, "last_name": lastName,
            "preferred_name": preferredName, "pronouns": pronouns,
            "email": email, "phone": phone,
            "address": address, "city": city, "state": state, "zip": zip,
            "country": country,
            "linkedin": linkedin, "github": github, "portfolio": portfolio,
            "school": school, "degree": degree, "discipline": discipline,
            "gpa": gpa, "grad_year": gradYear, "grad_month": gradMonth,
            "current_company": currentCompany, "current_title": currentTitle,
            "years_experience": years,
            "salary_expectation": salary, "start_date": startDate,
            "intern_season": internSeason,
            "work_arrangement": workArrangement, "how_heard": howHeard,
            "gender": gender, "race": race, "ethnicity": ethnicity,
            "veteran_status": veteranStatus, "disability_status": disabilityStatus,
            "work_authorized": workAuthorized,
            "needs_sponsorship": needsSponsorship,
            "willing_to_relocate": willingToRelocate,
            "background_check": backgroundCheck,
            "drug_test": drugTest,
            "over_18": over18,
            "can_travel": canTravel,
            "previously_applied": previouslyApplied,
            "related_to_employee": relatedToEmployee,
        ]
        var out: [String: Any] = [:]
        for key in keys {
            guard let value = all[key] else { continue }
            if let text = value as? String {
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if omitEmpty {
                    if !trimmed.isEmpty { out[key] = trimmed }
                } else {
                    out[key] = trimmed
                }
            } else {
                out[key] = value
            }
        }
        return out
    }

    /// Blank rows are dropped rather than sent: an empty entry the user added
    /// and did not fill in is not a degree, and the server would store it.
    var educationPayload: [[String: String]] {
        education.filter { !$0.isBlank }.map(\.payload)
    }

    /// ``includeEducation`` defaults off. Sending the list is destructive --
    /// an empty one clears every stored degree -- so only a screen that
    /// actually loaded and edited it may send it.
    func fullPayload(omitEmpty: Bool = false,
                     includeEducation: Bool = false) -> [String: Any] {
        var out = payload(keys: Set([
            "first_name", "last_name", "preferred_name", "pronouns",
            "email", "phone", "address", "city", "state", "zip", "country",
            "linkedin", "github", "portfolio",
            "school", "degree", "discipline", "gpa", "grad_year", "grad_month",
            "current_company", "current_title", "years_experience",
            "salary_expectation", "start_date", "intern_season",
            "work_arrangement", "how_heard",
            "gender", "race", "ethnicity", "veteran_status", "disability_status",
            "work_authorized", "needs_sponsorship", "willing_to_relocate",
            "background_check", "drug_test", "over_18", "can_travel",
            "previously_applied", "related_to_employee",
        ]), omitEmpty: omitEmpty)
        if includeEducation { out["education"] = educationPayload }
        return out
    }
}
