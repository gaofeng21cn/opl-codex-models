import Foundation
import XCTest
@testable import CodexModelManager

final class CustomModelEditorTests: XCTestCase {
    func testAddingClonesTemplateAndOverridesEditableFields() throws {
        let source = try JSONSerialization.data(withJSONObject: [
            "models": [[
                "slug": "template-model",
                "display_name": "Template",
                "description": "Template description",
                "context_window": 100_000,
                "max_context_window": 100_000,
                "input_modalities": ["text"],
                "supports_image_detail_original": false,
                "priority": 50,
                "wire_api": "responses",
                "supports_tools": true,
                "supported_reasoning_levels": [["effort": "high", "description": "Provider description"]],
                "default_reasoning_level": "high"
            ]]
        ])
        var draft = NewModelDraft()
        draft.slug = " Vendor-Vision "
        draft.displayName = " Vendor Vision "
        draft.description = " Hosted multimodal model "
        draft.templateSlug = "template-model"
        draft.contextWindow = 262_144
        draft.supportsImage = true
        draft.supportsOriginalImageDetail = true
        draft.reasoning = ReasoningSettings(supportedEfforts: ["low", "high", "max"], defaultEffort: "max")

        let updated = try CustomModelEditor.adding(
            draft: draft,
            to: source,
            existingSlugs: ["template-model"]
        )
        let root = try XCTUnwrap(
            JSONSerialization.jsonObject(with: updated) as? [String: Any]
        )
        let models = try XCTUnwrap(root["models"] as? [[String: Any]])
        let added = try XCTUnwrap(models.last)

        XCTAssertEqual(models.count, 2)
        XCTAssertEqual(added["slug"] as? String, "vendor-vision")
        XCTAssertEqual(added["display_name"] as? String, "Vendor Vision")
        XCTAssertEqual(added["description"] as? String, "Hosted multimodal model")
        XCTAssertEqual(added["context_window"] as? Int, 262_144)
        XCTAssertEqual(added["max_context_window"] as? Int, 262_144)
        XCTAssertEqual(added["input_modalities"] as? [String], ["text", "image"])
        XCTAssertEqual(added["supports_image_detail_original"] as? Bool, true)
        XCTAssertEqual(added["priority"] as? Int, 51)
        XCTAssertEqual(added["wire_api"] as? String, "responses")
        XCTAssertEqual(added["supports_tools"] as? Bool, true)
        let levels = try XCTUnwrap(added["supported_reasoning_levels"] as? [[String: String]])
        XCTAssertEqual(levels.map { $0["effort"] }, ["low", "high", "max"])
        XCTAssertEqual(levels[1]["description"], "Provider description")
        XCTAssertEqual(added["default_reasoning_level"] as? String, "max")
        XCTAssertEqual(models[0]["default_reasoning_level"] as? String, "high")
    }

    func testAddingRejectsDuplicateAndInvalidSlugs() throws {
        let source = try JSONSerialization.data(withJSONObject: [
            "models": [[
                "slug": "template-model",
                "display_name": "Template",
                "description": "Template description",
                "priority": 1
            ]]
        ])
        var draft = NewModelDraft()
        draft.slug = "template-model"
        draft.displayName = "Duplicate"
        draft.description = "Duplicate model"
        draft.templateSlug = "template-model"

        XCTAssertThrowsError(
            try CustomModelEditor.adding(
                draft: draft,
                to: source,
                existingSlugs: ["template-model"]
            )
        ) { error in
            XCTAssertTrue(error.localizedDescription.contains("已存在"))
        }

        draft.slug = "Invalid Slug"
        XCTAssertThrowsError(
            try CustomModelEditor.adding(
                draft: draft,
                to: source,
                existingSlugs: []
            )
        ) { error in
            XCTAssertTrue(error.localizedDescription.contains("slug"))
        }
    }

    func testAddingCanCloneAnOfficialTemplateIntoAnEmptyCustomSource() throws {
        let source = try JSONSerialization.data(withJSONObject: ["models": []])
        let catalog = try JSONSerialization.data(withJSONObject: [
            "models": [[
                "slug": "gpt-official",
                "visibility": "hide",
                "display_name": "GPT Official",
                "description": "Official template",
                "context_window": 200_000,
                "max_context_window": 800_000,
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": true,
                "priority": 1,
                "wire_api": "responses",
                "supported_reasoning_levels": [["effort": "high", "description": "High"]],
                "default_reasoning_level": "high"
            ]]
        ])
        var draft = NewModelDraft()
        draft.slug = "hosted-model"
        draft.displayName = "Hosted Model"
        draft.description = "Hosted model"
        draft.templateSlug = "gpt-official"
        draft.contextWindow = 128_000

        let updated = try CustomModelEditor.adding(
            draft: draft,
            to: source,
            catalogData: catalog,
            existingSlugs: ["gpt-official"]
        )
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: updated) as? [String: Any])
        let model = try XCTUnwrap((root["models"] as? [[String: Any]])?.first)
        XCTAssertEqual(model["slug"] as? String, "hosted-model")
        XCTAssertEqual(model["wire_api"] as? String, "responses")
        XCTAssertEqual(model["priority"] as? Int, 1000)
        XCTAssertEqual(model["visibility"] as? String, "list")
        XCTAssertEqual(model["default_reasoning_level"] as? String, "high")
        XCTAssertEqual((model["supported_reasoning_levels"] as? [[String: String]])?.first?["effort"], "high")
    }

    func testEditingReasoningPreservesOtherModelsAndUnknownMetadata() throws {
        let target: [String: Any] = [
            "slug": "vendor-model",
            "input_modalities": ["text", "image"],
            "context_window": 1_048_576,
            "provider_metadata": ["tools": ["custom-function"]],
            "supported_reasoning_levels": [
                ["effort": "high", "description": "Provider high", "budget": 200],
                ["effort": "future", "description": "Future provider effort", "budget": 400]
            ],
            "default_reasoning_level": "high"
        ]
        let other: [String: Any] = ["slug": "other", "default_reasoning_level": "medium"]
        let source = try JSONSerialization.data(withJSONObject: ["schema": "future.v2", "models": [target, other]])
        let settings = ReasoningSettings(supportedEfforts: ["low", "high", "future"], defaultEffort: "future")
        let updated = try CustomModelEditor.updatingReasoning(settings, for: "vendor-model", in: source)
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: updated) as? [String: Any])
        let models = try XCTUnwrap(root["models"] as? [[String: Any]])
        XCTAssertEqual(root["schema"] as? String, "future.v2")
        XCTAssertEqual(models[1] as NSDictionary, other as NSDictionary)
        var remaining = models[0]
        var original = target
        for key in ["supported_reasoning_levels", "default_reasoning_level"] {
            remaining.removeValue(forKey: key)
            original.removeValue(forKey: key)
        }
        XCTAssertEqual(remaining as NSDictionary, original as NSDictionary)
        let levels = try XCTUnwrap(models[0]["supported_reasoning_levels"] as? [[String: Any]])
        XCTAssertEqual(levels.map { $0["effort"] as? String }, settings.supportedEfforts)
        XCTAssertEqual(levels[2] as NSDictionary, (target["supported_reasoning_levels"] as! [[String: Any]])[1] as NSDictionary)
        XCTAssertEqual(models[0]["default_reasoning_level"] as? String, "future")
    }

    func testEditingRejectsInvalidSettingsAndModelsOutsideCustomSource() throws {
        let source = try JSONSerialization.data(withJSONObject: ["models": [["slug": "vendor-model"]]])
        for settings in [
            ReasoningSettings(),
            ReasoningSettings(supportedEfforts: ["low", "high"], defaultEffort: "max"),
            ReasoningSettings(supportedEfforts: ["high", "high"], defaultEffort: "high")
        ] {
            XCTAssertThrowsError(try CustomModelEditor.updatingReasoning(settings, for: "vendor-model", in: source))
        }
        XCTAssertThrowsError(try CustomModelEditor.updatingReasoning(
            ReasoningSettings(supportedEfforts: ["high"], defaultEffort: "high"), for: "gpt-official", in: source
        ))
    }

    func testRemovingDefaultSelectsRemainingEffortAndEmptySelectionIsInvalid() {
        var settings = ReasoningSettings(supportedEfforts: ["low", "high", "max"], defaultEffort: "high")
        settings.setEnabled(false, for: "high")
        XCTAssertEqual(settings.defaultEffort, "max")
        settings.setEnabled(false, for: "max")
        XCTAssertEqual(settings.defaultEffort, "low")
        settings.setEnabled(false, for: "low")
        XCTAssertFalse(settings.isValid)
        settings.setEnabled(true, for: "high")
        XCTAssertEqual(settings.defaultEffort, "high")
        XCTAssertTrue(settings.isValid)
    }
}
