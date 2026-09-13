import Foundation
import XCTest
@testable import CodexModelManager

final class CatalogParserTests: XCTestCase {
    func testParseModelsMarksCustomSlugsAndCapabilities() throws {
        let customData = try jsonData([
            "models": [["slug": "vendor-vision"]]
        ])
        let catalogData = try jsonData([
            "models": [
                [
                    "slug": "gpt-official",
                    "display_name": "GPT Official",
                    "input_modalities": ["text"],
                    "context_window": 200_000,
                    "max_context_window": 800_000,
                    "priority": 10
                ],
                [
                    "slug": "vendor-vision",
                    "display_name": "Vendor Vision",
                    "input_modalities": ["text", "image"],
                    "supports_image_detail_original": true,
                    "context_window": 262_144,
                    "max_context_window": 524_288,
                    "priority": 20,
                    "supported_reasoning_levels": [
                        ["effort": "low", "description": "Low"],
                        ["effort": "high", "description": "High"],
                        ["effort": "max", "description": "Max"]
                    ],
                    "default_reasoning_level": "high"
                ]
            ]
        ])

        let models = try CatalogParser.parseModels(
            customData: customData,
            catalogData: catalogData
        )

        XCTAssertEqual(models.count, 2)
        XCTAssertEqual(models[0].source, .official)
        XCTAssertEqual(models[1].source, .custom)
        XCTAssertEqual(models[1].inputModalities, ["text", "image"])
        XCTAssertTrue(models[1].supportsOriginalImageDetail)
        XCTAssertEqual(models[1].contextWindow, 262_144)
        XCTAssertEqual(models[1].maxContextWindow, 524_288)
        XCTAssertEqual(models[0].maxContextWindow, 800_000)
        XCTAssertEqual(models[1].reasoning.supportedEfforts, ["low", "high", "max"])
        XCTAssertEqual(models[1].reasoning.defaultEffort, "high")
    }

    func testParseRecordsSupportsTimestampedAndLegacyLines() {
        let text = """
        {"recorded_at":"2026-08-27T07:15:21Z","status":"no_change","app_version":"0.1.0","bundled_count":10,"custom_count":8,"current_hash":"abc","desired_hash":"abc"}
        {"status":"updated","app_version":"0.0.9","bundled_count":9,"custom_count":7,"current_hash":"old","desired_hash":"new"}
        ignored output
        """

        let records = CatalogParser.parseRecords(text)

        XCTAssertEqual(records.count, 2)
        XCTAssertEqual(records[0].status, "updated")
        XCTAssertNil(records[0].recordedAt)
        XCTAssertEqual(records[1].status, "no_change")
        XCTAssertNotNil(records[1].recordedAt)
        XCTAssertEqual(records[1].totalCount, 18)
        XCTAssertTrue(records[1].hashesMatch)
    }

    private func jsonData(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }
}
