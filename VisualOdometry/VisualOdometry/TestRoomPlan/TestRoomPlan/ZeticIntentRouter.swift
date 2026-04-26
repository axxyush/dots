import Foundation

/// Result from intent resolution — either a navigation destination or a Q&A answer.
enum IntentResult {
    /// User wants to navigate somewhere.
    case navigate(destinationName: String)
    /// User asked a question — reply with this text.
    case answer(text: String)
    /// Could not resolve anything.
    case unknown
}

/// Intent router for navigation requests and room-aware questions.
/// It uses lightweight keyword routing for destinations and Gemini-backed
/// Q&A when the user asks about the saved room model.
struct ZeticIntentRouter: Sendable {
    // MARK: - Unified Intent Resolution

    /// Resolves user text into either a navigation command or a Q&A answer.
    func resolveIntent(
        userText: String,
        destinations: [String],
        roomContext: String = "",
        roomContextJSON: String = ""
    ) async -> IntentResult {
        // First check if it's a question about the room
        if isRoomQuestion(userText) {
            if let answer = await answerRoomQuestion(
                userText: userText,
                roomContext: roomContext,
                roomContextJSON: roomContextJSON
            ) {
                return .answer(text: answer)
            }
            // Fallback: try to answer from room context offline
            if let offlineAnswer = offlineRoomAnswer(userText: userText, roomContext: roomContext) {
                return .answer(text: offlineAnswer)
            }
            return .answer(text: "I couldn't find that in the saved room model yet.")
        }

        // Try LLM navigation intent
        if let llmResult = await runLLMIntent(userText: userText, destinations: destinations) {
            return .navigate(destinationName: llmResult)
        }

        // Keyword fallback for navigation
        if let keyword = keywordMatch(userText: userText, destinations: destinations) {
            return .navigate(destinationName: keyword)
        }

        return .unknown
    }

    /// Simple resolve that returns just the destination name (backward compat).
    func resolveDestination(userText: String, destinations: [String]) async -> String? {
        let result = await resolveIntent(userText: userText, destinations: destinations)
        if case .navigate(let name) = result { return name }
        return nil
    }

    // MARK: - Question Detection

    private func isRoomQuestion(_ text: String) -> Bool {
        let lowered = text.lowercased()
        let questionKeywords = [
            "how many", "what", "which", "where", "is there", "are there",
            "tell me about", "describe", "list", "count", "any", "do we have",
            "what's", "where's", "can you tell", "how far", "near"
        ]
        return text.contains("?") || questionKeywords.contains { lowered.contains($0) }
    }

    // MARK: - Room Q&A

    private func answerRoomQuestion(userText: String, roomContext: String, roomContextJSON: String) async -> String? {
        guard !roomContext.isEmpty || !roomContextJSON.isEmpty else { return nil }
        let gemini = GeminiRoomAnswerService()
        return await gemini.answer(
            question: userText,
            roomSummary: roomContext,
            roomJSON: roomContextJSON
        )
    }

    // MARK: - Offline Room Answers (No LLM fallback)

    private func offlineRoomAnswer(userText: String, roomContext: String) -> String? {
        let lowered = userText.lowercased()
        let lines = roomContext.components(separatedBy: "\n")
        guard !lines.isEmpty else { return nil }

        // "How many objects/doors/windows?"
        if lowered.contains("how many") {
            if lowered.contains("door") {
                if let doorLine = lines.first(where: { $0.contains("Doors:") }),
                   let count = extractCount(from: doorLine, key: "Doors:") {
                    return "There are \(count) doors in this room."
                }
            }
            if lowered.contains("window") {
                if let windowLine = lines.first(where: { $0.contains("Windows:") }),
                   let count = extractCount(from: windowLine, key: "Windows:") {
                    return "There are \(count) windows in this room."
                }
            }
            if lowered.contains("object") || lowered.contains("thing") || lowered.contains("item") {
                if let objLine = lines.first(where: { $0.contains("Objects:") }),
                   let count = extractCount(from: objLine, key: "Objects:") {
                    return "There are \(count) objects in this room."
                }
            }
            if lowered.contains("wall") {
                if let wallLine = lines.first(where: { $0.contains("Walls:") }),
                   let count = extractCount(from: wallLine, key: "Walls:") {
                    return "There are \(count) walls in this room."
                }
            }
        }

        // "What objects are in the room?"
        if lowered.contains("what") && (lowered.contains("object") || lowered.contains("furniture") || lowered.contains("in the room")) {
            let objects = lines.filter { $0.hasPrefix("- Object:") }
                .compactMap { $0.components(separatedBy: ":").dropFirst().first?.trimmingCharacters(in: .whitespaces) }
                .map { $0.components(separatedBy: " (").first ?? $0 }
            if !objects.isEmpty {
                return "I can see: \(objects.joined(separator: ", "))."
            }
        }

        // "How big is the room?"
        if lowered.contains("dimension") || lowered.contains("big") || lowered.contains("size") {
            if let dimLine = lines.first(where: { $0.contains("Room dimensions:") }) {
                return dimLine
            }
        }

        return nil
    }

    private func extractCount(from line: String, key: String) -> Int? {
        guard let range = line.range(of: key) else { return nil }
        let afterKey = line[range.upperBound...].trimmingCharacters(in: .whitespaces)
        let numStr = afterKey.prefix(while: { $0.isNumber })
        return Int(numStr)
    }

    // MARK: - LLM Navigation Intent

    private func runLLMIntent(userText: String, destinations: [String]) async -> String? {
        // LLM implementation removed.
        return nil
    }

    // MARK: - Keyword Fallback

    /// Simple keyword matching: checks if any destination name appears in the user text,
    /// or if common synonyms map to known categories.
    private func keywordMatch(userText: String, destinations: [String]) -> String? {
        let lowered = userText.lowercased()

        // Direct name match
        for dest in destinations {
            if lowered.contains(dest.lowercased()) {
                return dest
            }
        }

        // Synonym mapping
        let synonyms: [(keywords: [String], categories: [String])] = [
            (["wash", "hands", "restroom", "toilet", "bathroom", "wc"], ["bathroom", "toilet", "restroom"]),
            (["sit", "couch", "sofa", "relax"], ["sofa", "couch", "chair"]),
            (["eat", "food", "dining", "table", "meal"], ["dining table", "table", "kitchen"]),
            (["sleep", "rest", "bed", "nap", "bedroom"], ["bed", "bedroom"]),
            (["cook", "kitchen", "stove", "fridge"], ["kitchen", "stove", "refrigerator"]),
            (["exit", "leave", "door", "entrance", "front"], ["door", "entrance", "exit"]),
            (["tv", "watch", "television", "screen"], ["tv", "television"]),
        ]

        for (keywords, categories) in synonyms {
            if keywords.contains(where: { lowered.contains($0) }) {
                for dest in destinations {
                    let destLower = dest.lowercased()
                    if categories.contains(where: { destLower.contains($0) }) {
                        return dest
                    }
                }
            }
        }

        return nil
    }

    /// Fuzzy-match LLM output against known destination names.
    private func matchDestination(from output: String, candidates: [String]) -> String? {
        let outputLower = output.lowercased()

        // Exact match first
        for candidate in candidates {
            if outputLower == candidate.lowercased() {
                return candidate
            }
        }

        // Substring match
        for candidate in candidates {
            if outputLower.contains(candidate.lowercased()) {
                return candidate
            }
        }

        // Reverse: check if candidate appears in the output
        for candidate in candidates {
            if candidate.lowercased().contains(outputLower) && !outputLower.isEmpty {
                return candidate
            }
        }

        return nil
    }
}
