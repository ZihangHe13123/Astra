@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import Testing

@Test func pointerSafeRegionBoundsRequireExactSnapshotMatch() {
    let snapshot = CGRect(x: 10, y: 20, width: 300, height: 200)

    #expect(pointerSafeRegionBoundsMatch(snapshot, snapshot))
    #expect(!pointerSafeRegionBoundsMatch(
        CGRect(x: 10.5, y: 20, width: 300, height: 200),
        snapshot
    ))
}

@Test func screenCaptureShadowBoundsMatchAccessibilityWindowEdges() {
    let screenCapture = CGRect(x: 99, y: 169, width: 719, height: 714)
    let accessibility = CGRect(x: 100, y: 170, width: 717, height: 712)

    #expect(screenCaptureBoundsMatchAXBounds(
        screenCapture: screenCapture,
        accessibility: accessibility
    ))
    #expect(screenCaptureBoundsMatchAXBounds(
        screenCapture: accessibility,
        accessibility: accessibility
    ))
}

@Test func screenCaptureBoundsRejectTwoPointEdgeDriftAndInvalidRects() {
    let accessibility = CGRect(x: 100, y: 170, width: 717, height: 712)

    #expect(!screenCaptureBoundsMatchAXBounds(
        screenCapture: CGRect(x: 98, y: 169, width: 720, height: 714),
        accessibility: accessibility
    ))
    #expect(!screenCaptureBoundsMatchAXBounds(
        screenCapture: CGRect(x: CGFloat.infinity, y: 169, width: 719, height: 714),
        accessibility: accessibility
    ))
    #expect(!screenCaptureBoundsMatchAXBounds(
        screenCapture: CGRect(x: 100, y: 170, width: 0, height: 712),
        accessibility: accessibility
    ))
}

@Test func coordinatesRemainWindowLocalAcrossBackingScale() {
    let geometry = WindowGeometry(bounds: CGRect(x: 100, y: 200, width: 400, height: 300), backingScale: 2)

    #expect(geometry.screenPoint(for: CGPoint(x: 50, y: 75)) == CGPoint(x: 150, y: 275))
    #expect(geometry.pixelSize == CGSize(width: 800, height: 600))
}

@Test func artifactWriterRejectsPathEscapeName() throws {
    #expect(throws: CapturePathError.self) {
        try ArtifactDirectory.writePNG(Data(), named: "../window.png")
    }
}

@Test func artifactBundlePublishesBothDurableFilesOnlyAfterBothAreComplete() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor)
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(1)

        let result = try publisher.publish(
            image: Data("png".utf8),
            imageName: names.image,
            detail: Data("detail".utf8),
            detailName: names.detail
        )

        #expect(result == .durable)
        #expect(try Data(contentsOf: root.appendingPathComponent(names.image)) == Data("png".utf8))
        #expect(try Data(contentsOf: root.appendingPathComponent(names.detail)) == Data("detail".utf8))
        #expect(try bundleDirectoryEntries(root).filter { $0.hasSuffix(".tmp") }.isEmpty)
        #expect(syscalls.renameCalls == 2)
        #expect(syscalls.fileSyncCalls == 2)
        #expect(syscalls.directorySyncCalls == 1)
        #expect(syscalls.firstRenameEventIndex.map { index in
            syscalls.events[..<index].filter { $0 == "file-sync" }.count == 2
        } == true)
        for name in [names.image, names.detail] {
            let mode = try FileManager.default.attributesOfItem(
                atPath: root.appendingPathComponent(name).path
            )[.posixPermissions] as? NSNumber
            #expect(mode?.intValue == 0o600)
        }
    }
}

@Test func artifactBundleRollsBackEveryPreDurabilityFailure() throws {
    let cases: [(BundleTestArtifactSyscalls.Fault, ArtifactPublicationStage)] = [
        (.open(1), .open), (.open(2), .open),
        (.descriptorStatus(1), .open), (.descriptorStatus(2), .open),
        (.descriptorStatus(3), .open), (.descriptorStatus(4), .open),
        (.fileMode(1), .open), (.fileMode(2), .open),
        (.specialFileMode(1), .open), (.specialFileMode(2), .open),
        (.write(1), .write), (.write(2), .write),
        (.fileSync(1), .fileSync), (.fileSync(2), .fileSync),
        (.close(1), .close), (.close(2), .close),
        (.rename(1), .publish), (.rename(2), .publish),
        (.directorySync, .directorySync),
    ]
    for (index, testCase) in cases.enumerated() {
        try withBundleTestDirectory { root, descriptor in
            let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: testCase.0)
            let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
            let names = smartBundleNames(index + 1)
            do {
                _ = try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
                Issue.record("expected bundle publication failure for \(testCase.0)")
            } catch let error as ArtifactPublicationError {
                #expect(error.stage == testCase.1)
            } catch {
                Issue.record("unexpected failure for case \(index) \(testCase.0): \(error)")
            }
            let entries = try bundleDirectoryEntries(root)
            #expect(!entries.contains(names.image))
            #expect(!entries.contains(names.detail))
            #expect(entries.allSatisfy { $0.hasSuffix(".quarantine") })
            #expect(syscalls.unlinkCalls == 0)
        }
    }
}

@Test func artifactBundleRejectsCollisionSymlinkHardlinkAndWrongNamesWithoutPartialPublish() throws {
    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        let collision = smartBundleNames(1)
        try Data("existing".utf8).write(to: root.appendingPathComponent(collision.detail))
        #expect(throws: (any Error).self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: collision.image,
                detail: Data("detail".utf8), detailName: collision.detail
            )
        }
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent(collision.image).path))
        #expect(try Data(contentsOf: root.appendingPathComponent(collision.detail)) == Data("existing".utf8))
    }

    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        let symlink = smartBundleNames(2)
        try FileManager.default.createSymbolicLink(
            at: root.appendingPathComponent(symlink.image),
            withDestinationURL: URL(fileURLWithPath: "/dev/null")
        )
        #expect(throws: (any Error).self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: symlink.image,
                detail: Data("detail".utf8), detailName: symlink.detail
            )
        }
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent(symlink.detail).path))
    }

    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        let hardlink = smartBundleNames(3)
        let outside = root.appendingPathComponent("outside")
        try Data("outside".utf8).write(to: outside)
        try FileManager.default.linkItem(at: outside, to: root.appendingPathComponent(hardlink.detail))
        #expect(throws: (any Error).self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: hardlink.image,
                detail: Data("detail".utf8), detailName: hardlink.detail
            )
        }
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent(hardlink.image).path))
        #expect(try Data(contentsOf: outside) == Data("outside".utf8))
    }

    try withBundleTestDirectory { _, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        let wrong = smartBundleNames(4)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: wrong.image,
                detail: Data("detail".utf8), detailName: "wrong.ax.json"
            )
        }
    }
}

@Test func artifactBundleRevalidatesExactDirectoryBeforePublishingFinalNames() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .unsafeDirectoryAfterFileSync
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(21)

        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let remainingEntries = try bundleDirectoryEntries(root)
        #expect(remainingEntries.count == 2)
        #expect(remainingEntries.allSatisfy { $0.hasSuffix(".quarantine") })
        #expect(syscalls.unlinkCalls == 0)
        #expect(syscalls.finalRenameCalls == 0)
    }
}

@Test func artifactBundleQuotaNeverDeletesPublishedArtifacts() throws {
    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor),
            maximumDetailArtifacts: 2,
            maximumDetailBytes: 5
        )
        for (index, detail) in [(1, "aa"), (2, "bbb")] {
            let names = smartBundleNames(index)
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data(detail.utf8), detailName: names.detail
            )
        }
        let overCount = smartBundleNames(3)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: overCount.image,
                detail: Data("x".utf8), detailName: overCount.detail
            )
        }
        #expect(try bundleDirectoryEntries(root).filter { $0.hasPrefix("snapshot-") }.count == 4)
        publisher.close()
        #expect(try bundleDirectoryEntries(root).filter { $0.hasPrefix("snapshot-") }.count == 4)
    }

    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor),
            maximumDetailArtifacts: 8,
            maximumDetailBytes: 5
        )
        let first = smartBundleNames(4)
        _ = try publisher.publish(
            image: Data("png".utf8), imageName: first.image,
            detail: Data("four".utf8), detailName: first.detail
        )
        let overBytes = smartBundleNames(5)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: overBytes.image,
                detail: Data("xx".utf8), detailName: overBytes.detail
            )
        }
        #expect(Set(try bundleDirectoryEntries(root)) == [first.image, first.detail])
    }
}

@Test func artifactBundlePreservesStandalonePNGCrashResidualForTask4() throws {
    try withBundleTestDirectory { root, descriptor in
        let residual = smartBundleNames(27)
        try Data("png-only".utf8).write(to: root.appendingPathComponent(residual.image))
        #expect(chmod(root.appendingPathComponent(residual.image).path, mode_t(0o600)) == 0)

        let publisher = ArtifactBundlePublisher(directoryFD: descriptor)
        let current = smartBundleNames(28)
        _ = try publisher.publish(
            image: Data("png".utf8), imageName: current.image,
            detail: Data("detail".utf8), detailName: current.detail
        )

        #expect(try Data(contentsOf: root.appendingPathComponent(residual.image)) == Data("png-only".utf8))
        #expect(Set(try bundleDirectoryEntries(root)) == [residual.image, current.image, current.detail])
    }
}

@Test func artifactBundleRequiresExact0700DirectoryAndDefaultsToEightDetails() throws {
    for unsafeMode in [mode_t(0o755), mode_t(0o1700)] {
        try withBundleTestDirectory { root, descriptor in
            #expect(chmod(root.path, unsafeMode) == 0)
            let publisher = ArtifactBundlePublisher(
                directoryFD: descriptor,
                syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
            )
            let names = smartBundleNames(15)
            #expect(throws: ArtifactBundleError.self) {
                try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
            }
            let unsafeDirectoryEntries = try bundleDirectoryEntries(root)
            #expect(unsafeDirectoryEntries.isEmpty)
        }
    }

    try withBundleTestDirectory { root, descriptor in
        let publisher = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        for index in 1...8 {
            let names = smartBundleNames(index)
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let ninth = smartBundleNames(9)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: ninth.image,
                detail: Data("detail".utf8), detailName: ninth.detail
            )
        }
        let retainedEntries = try bundleDirectoryEntries(root)
        #expect(retainedEntries.count == 16)
    }
}

@Test func artifactBundleRecoversQuotaFromHeldDirectoryAndFailsClosedOnResiduals() throws {
    try withBundleTestDirectory { root, descriptor in
        let first = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
        )
        let names = smartBundleNames(8)
        _ = try first.publish(
            image: Data("png".utf8), imageName: names.image,
            detail: Data("detail".utf8), detailName: names.detail
        )
        let recovered = ArtifactBundlePublisher(
            directoryFD: descriptor,
            syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor),
            maximumDetailArtifacts: 1,
            maximumDetailBytes: 64 * 1024 * 1024
        )
        let over = smartBundleNames(9)
        #expect(throws: ArtifactBundleError.self) {
            try recovered.publish(
                image: Data("png".utf8), imageName: over.image,
                detail: Data("detail".utf8), detailName: over.detail
            )
        }
        #expect(Set(try bundleDirectoryEntries(root)) == [names.image, names.detail])
    }

    for residualName in [".abandoned.tmp", "malformed.ax.json"] {
        try withBundleTestDirectory { root, descriptor in
            try Data("residual".utf8).write(to: root.appendingPathComponent(residualName))
            let publisher = ArtifactBundlePublisher(
                directoryFD: descriptor,
                syscalls: BundleTestArtifactSyscalls(directoryFD: descriptor)
            )
            let names = smartBundleNames(10)
            #expect(throws: ArtifactBundleError.self) {
                try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
            }
            #expect(Set(try bundleDirectoryEntries(root)) == [residualName])
        }
    }
}

@Test func artifactBundlePoisonIsExplicitAfterSecondRenameOrUncertainRollback() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .rename(2))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(11)
        do {
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
            Issue.record("second rename failure unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            #expect(error.stage == .publish)
            #expect(error.publisherPoisoned)
        }
        let retry = smartBundleNames(12)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: retry.image,
                detail: Data("detail".utf8), detailName: retry.detail
            )
        }
        let retained = try bundleDirectoryEntries(root)
        #expect(retained.count == 2)
        #expect(retained.allSatisfy { $0.hasSuffix(".quarantine") })
        #expect(syscalls.unlinkCalls == 0)
    }

    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .write(1)
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(13)
        do {
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
            Issue.record("uncertain rollback unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            #expect(error.stage == .write)
            #expect(error.publisherPoisoned)
        }
        let retry = smartBundleNames(14)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: retry.image,
                detail: Data("detail".utf8), detailName: retry.detail
            )
        }
        publisher.close()
    }

    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .close(1))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let first = smartBundleNames(18)
        do {
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: first.image,
                detail: Data("detail".utf8), detailName: first.detail
            )
            Issue.record("uncertain close unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            #expect(error.stage == .close)
            #expect(error.publisherPoisoned)
        }
        let retry = smartBundleNames(19)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: retry.image,
                detail: Data("detail".utf8), detailName: retry.detail
            )
        }
    }
}

@Test func artifactBundleFailsClosedWhenRecoveredDirectoryEnumerationFails() throws {
    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .directoryRead)
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(20)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        #expect(syscalls.openCalls == 0)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
    }
}

@Test func artifactBundleQuotaScanDoesNotChangeTheHeldDirectoryOffset() throws {
    try withBundleTestDirectory { _, descriptor in
        let before = Darwin.lseek(descriptor, 0, SEEK_CUR)
        #expect(before >= 0)
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor)
        let names = smartBundleNames(23)
        _ = try publisher.publish(
            image: Data("png".utf8), imageName: names.image,
            detail: Data("detail".utf8), detailName: names.detail
        )
        let after = Darwin.lseek(descriptor, 0, SEEK_CUR)
        #expect(after == before)
    }
}

@Test func artifactBundleQuotaScanPoisonsOnEntryOrNameByteBudgetOverflow() throws {
    let cases: [(entryLimit: Int, nameByteLimit: Int, names: [String])] = [
        (2, 1_024, ["one", "two", "three"]),
        (16, 5, ["aaaa", "bbbb"]),
    ]
    for testCase in cases {
        try withBundleTestDirectory { root, descriptor in
            for name in testCase.names {
                try Data("x".utf8).write(to: root.appendingPathComponent(name))
            }
            let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor)
            let publisher = ArtifactBundlePublisher(
                directoryFD: descriptor,
                syscalls: syscalls,
                maximumDirectoryEntries: testCase.entryLimit,
                maximumDirectoryNameBytes: testCase.nameByteLimit
            )
            let names = smartBundleNames(24)
            #expect(throws: ArtifactBundleError.self) {
                try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
            }
            #expect(syscalls.openCalls == 0)
            #expect(throws: ArtifactBundleError.self) {
                try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
            }
        }
    }
}

@Test func artifactBundleRollbackNeverUnlinksANameReplacedAfterStatus() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .replaceRollbackTarget
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(22)

        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }

        let entries = try bundleDirectoryEntries(root)
        #expect(entries.contains(".attacker-stolen"))
        let quarantines = entries.filter { $0.hasSuffix(".quarantine") }
        #expect(quarantines.count == 2)
        #expect(try quarantines.contains {
            try Data(contentsOf: root.appendingPathComponent($0)) == Data("replacement".utf8)
        })
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
    }
}

@Test func artifactBundleRetainsQuarantineWhenItsNameIsReplacedAfterStatus() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .replaceQuarantineAfterStatus
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(29)

        do {
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
            Issue.record("quarantine replacement probe unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            #expect(error.publisherPoisoned)
            #expect(!error.finalVisible)
        }

        let entries = try bundleDirectoryEntries(root)
        #expect(!entries.contains(names.image))
        #expect(!entries.contains(names.detail))
        #expect(entries.contains(".attacker-stolen"))
        let quarantines = entries.filter { $0.hasSuffix(".quarantine") }
        #expect(quarantines.count == 2)
        #expect(try quarantines.contains {
            try Data(contentsOf: root.appendingPathComponent($0)) == Data("replacement".utf8)
        })
        #expect(syscalls.unlinkCalls == 0)
        #expect(throws: ArtifactBundleError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
    }
}

@Test func artifactBundleTerminalCleanupResolvesAmbiguousCloseWithoutBlindlyClosingReusedFD() throws {
    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .closeStillOpen(1))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(25)
        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let ambiguousFD = try #require(syscalls.failedCloseFD)
        #expect(syscalls.injectedCloseErrnos == [EINTR])
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        #expect(syscalls.postFailureStatusCalls == 0)
        publisher.close()
        #expect(syscalls.postFailureStatusCalls == 1)
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) == -1)
        #expect(syscalls.failedCloseFDRecoveryCloseCalls == 1)
    }

    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .close(1))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(26)
        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let ambiguousFD = try #require(syscalls.failedCloseFD)
        #expect(syscalls.injectedCloseErrnos == [EINTR])
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) == -1)
        let replacement = Darwin.open("/dev/null", O_RDONLY | O_CLOEXEC)
        #expect(replacement >= 0)
        if replacement >= 0, replacement != ambiguousFD {
            #expect(Darwin.dup2(replacement, ambiguousFD) == ambiguousFD)
            _ = Darwin.close(replacement)
        }
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        publisher.close()
        #expect(syscalls.postFailureStatusCalls == 1)
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        #expect(syscalls.failedCloseFDRecoveryCloseCalls == 0)
        _ = Darwin.close(ambiguousFD)
    }

    try withBundleTestDirectory { _, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .closeAndReportEBADF(1))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(31)
        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let ambiguousFD = try #require(syscalls.failedCloseFD)
        #expect(syscalls.injectedCloseErrnos == [EINTR])
        publisher.close()
        #expect(syscalls.postFailureStatusCalls == 1)
        #expect(syscalls.failedCloseFDRecoveryCloseCalls == 0)
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) == -1)
    }

    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: .closeStillOpen(1))
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(32)
        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let ambiguousFD = try #require(syscalls.failedCloseFD)
        for quarantine in try bundleDirectoryEntries(root).filter({ $0.hasSuffix(".quarantine") }) {
            try FileManager.default.removeItem(at: root.appendingPathComponent(quarantine))
        }
        var unlinkedStatus = stat()
        #expect(Darwin.fstat(ambiguousFD, &unlinkedStatus) == 0)
        #expect(unlinkedStatus.st_nlink == 0)

        publisher.close()

        #expect(syscalls.postFailureStatusCalls == 1)
        #expect(syscalls.failedCloseFDRecoveryCloseCalls == 0)
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        _ = Darwin.close(ambiguousFD)
    }
}

@Test func artifactBundleTracksUnknownAmbiguousDescriptorWithoutBlindRecovery() throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .initialAndRecoveryStatusFailingCloseStillOpen
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(30)

        do {
            _ = try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
            Issue.record("unknown close authority probe unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            #expect(error.stage == .open)
            #expect(error.publisherPoisoned)
        }
        let ambiguousFD = try #require(syscalls.failedCloseFD)
        #expect(syscalls.injectedCloseErrnos == [EINTR])
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        let statusCallsBeforeClose = syscalls.postFailureStatusCalls

        publisher.close()

        #expect(syscalls.postFailureStatusCalls == statusCallsBeforeClose + 1)
        #expect(syscalls.failedCloseFDRecoveryCloseCalls == 0)
        #expect(Darwin.fcntl(ambiguousFD, F_GETFD) != -1)
        #expect(try bundleDirectoryEntries(root).contains { $0.hasSuffix(".quarantine") })
        _ = Darwin.close(ambiguousFD)
    }
}

@Test(arguments: [false, true])
func artifactBundleCloseAndEOFPreserveRetainedQuarantines(closeAcknowledged: Bool) throws {
    try withBundleTestDirectory { root, descriptor in
        let syscalls = BundleTestArtifactSyscalls(
            directoryFD: descriptor,
            fault: .write(1)
        )
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
        let names = smartBundleNames(closeAcknowledged ? 6 : 7)
        #expect(throws: ArtifactPublicationError.self) {
            try publisher.publish(
                image: Data("png".utf8), imageName: names.image,
                detail: Data("detail".utf8), detailName: names.detail
            )
        }
        let abandonedEntries = try bundleDirectoryEntries(root)
        #expect(abandonedEntries.contains { $0.hasSuffix(".quarantine") })

        var delivered = false
        _ = runHelperLoop(
            next: {
                guard closeAcknowledged, !delivered else { return nil }
                delivered = true
                return .line(Data("close".utf8))
            },
            handle: { _ in
                HelperResponse(
                    protocolVersion: protocolVersion,
                    requestID: "close",
                    ok: true,
                    result: .object(["closed": .bool(true)]),
                    snapshot: nil,
                    error: nil
                )
            },
            write: { _ in },
            cleanup: { publisher.close() },
            report: { _ in }
        )
        let cleanedEntries = try bundleDirectoryEntries(root)
        #expect(cleanedEntries == abandonedEntries)
        #expect(cleanedEntries.allSatisfy { $0.hasSuffix(".quarantine") })
        #expect(syscalls.unlinkCalls == 0)
    }
}

@Test func snapshotArtifactSessionOffPublishesOnlyTheLegacyPNG() throws {
    var pngWrites: [(Data, String)] = []
    let session = SnapshotArtifactSession(
        publishPNG: { pngWrites.append(($0, $1)) },
        bundlePublisher: nil
    )

    try session.publish(
        png: Data("png".utf8),
        named: "legacy.png",
        textDetail: .off,
        detail: nil
    )

    #expect(pngWrites.count == 1)
    #expect(pngWrites.first?.0 == Data("png".utf8))
    #expect(pngWrites.first?.1 == "legacy.png")
}

@Test func smartSnapshotMetadataIsBoundedAndBoundToTheSameSnapshot() throws {
    let envelope = AXTextDetailEnvelope(
        data: Data("detail".utf8),
        nodeCount: 4,
        maxDepthObserved: 2,
        truncated: true,
        truncationReasons: ["node_limit"]
    )

    guard case let .object(metadata) = smartSnapshotMetadata(
        envelope,
        snapshotID: "snapshot_1"
    ) else {
        Issue.record("metadata must be an object")
        return
    }
    #expect(Set(metadata.keys) == [
        "schema_version", "snapshot_id", "coverage", "node_count",
        "max_depth_observed", "byte_count", "sha256", "truncated",
        "truncation_reasons",
    ])
    if case let .string(snapshotID)? = metadata["snapshot_id"] { #expect(snapshotID == "snapshot_1") }
    else { Issue.record("metadata snapshot_id must be a string") }
    if case let .number(byteCount)? = metadata["byte_count"] { #expect(byteCount == 6) }
    else { Issue.record("metadata byte_count must be a number") }
    if case let .string(digest)? = metadata["sha256"] {
        #expect(digest == "9c0211c51d04574fdaee6d51f53f41952570b0bf68a7219aaae01ce00fa6b8dd")
    } else { Issue.record("metadata sha256 must be a string") }
    if case let .array(reasons)? = metadata["truncation_reasons"],
       reasons.count == 1,
       case let .string(reason) = reasons[0]
    {
        #expect(reason == "node_limit")
    } else { Issue.record("metadata truncation reasons must be exact") }
}

@Test func documentPathAcceptsOnlyAbsoluteLocalFileLocations() {
    #expect(normalizedDocumentPath("file:///private/tmp/astra%20copy.docx") == "/private/tmp/astra copy.docx")
    #expect(normalizedDocumentPath("/private/tmp/astra-copy.docx") == "/private/tmp/astra-copy.docx")
    #expect(normalizedDocumentPath("https://example.test/private.docx") == nil)
    #expect(normalizedDocumentPath("relative/private.docx") == nil)
}

@Test func focusedTransientMustBeStrictlyContainedByTheLockedWindow() {
    let target = CGRect(x: 100, y: 100, width: 800, height: 600)

    #expect(trustedFocusedWindow(
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 10, focusedBounds: target
    ))
    #expect(trustedFocusedWindow(
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 11, focusedBounds: CGRect(x: 250, y: 250, width: 300, height: 80)
    ))
    #expect(!trustedFocusedWindow(
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 11, focusedBounds: target
    ))
    #expect(!trustedFocusedWindow(
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 11, focusedBounds: CGRect(x: 50, y: 250, width: 300, height: 80)
    ))
}

@Test func frontmostContainedModalCanReuseTargetWithoutRaisingUnderlyingWindow() {
    let target = CGRect(x: 100, y: 100, width: 800, height: 600)
    let modal = CGRect(x: 250, y: 200, width: 400, height: 250)

    #expect(reusableFrontmostTarget(
        frontmostPID: 42, targetPID: 42,
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 11, focusedBounds: modal
    ))
    #expect(!reusableFrontmostTarget(
        frontmostPID: 43, targetPID: 42,
        expectedIdentity: 10, targetBounds: target,
        focusedIdentity: 11, focusedBounds: modal
    ))
}

@Test func snapshotRejectsTransitionBetweenContainedTransientRoots() {
    let target = CGRect(x: 100, y: 100, width: 800, height: 600)

    #expect(!trustedFocusedTransition(
        expectedIdentity: 10,
        targetBounds: target,
        beforeIdentity: 11,
        beforeBounds: CGRect(x: 200, y: 180, width: 500, height: 300),
        afterIdentity: 12,
        afterBounds: CGRect(x: 300, y: 220, width: 300, height: 180)
    ))
    #expect(!trustedFocusedTransition(
        expectedIdentity: 10,
        targetBounds: target,
        beforeIdentity: 11,
        beforeBounds: CGRect(x: 200, y: 180, width: 500, height: 300),
        afterIdentity: 12,
        afterBounds: CGRect(x: 50, y: 220, width: 300, height: 180)
    ))
}

@Test func focusedElementFallbackRequiresAContainedCompleteNonSecureTextControl() {
    let target = CGRect(x: 100, y: 100, width: 800, height: 600)
    let contained = CGRect(x: 250, y: 250, width: 300, height: 30)
    let textField = BoundedAXStringResult(value: "AXTextField", status: .complete)
    let noSubrole = BoundedAXStringResult(value: nil, status: .complete)

    #expect(trustedFocusedElement(
        role: textField, subrole: noSubrole, enabled: nil,
        targetBounds: target, focusedBounds: contained
    ))
    #expect(!trustedFocusedElement(
        role: textField,
        subrole: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
        enabled: true, targetBounds: target, focusedBounds: contained
    ))
    #expect(!trustedFocusedElement(
        role: BoundedAXStringResult(value: nil, status: .complete),
        subrole: noSubrole, enabled: true,
        targetBounds: target, focusedBounds: contained
    ))
    #expect(!trustedFocusedElement(
        role: textField, subrole: noSubrole, enabled: true,
        targetBounds: target, focusedBounds: CGRect(x: 50, y: 250, width: 300, height: 30)
    ))
}

@Test func keyboardFocusCaptureRequiresExactPIDAndStrictWindowContainment() {
    let container = CGRect(x: 100, y: 100, width: 800, height: 600)
    let role = BoundedAXStringResult(value: "AXTextArea", status: .complete)
    let subrole = BoundedAXStringResult(value: nil, status: .complete)
    let valid = makeKeyboardFocusObservation(
        expectedPID: 42,
        actualPID: 42,
        identityToken: "ax:focus",
        bounds: CGRect(x: 150, y: 150, width: 300, height: 200),
        role: role,
        subrole: subrole,
        enabled: true,
        containerBounds: container
    )
    #expect(valid.authority?.identityToken == "ax:focus")

    let rejected: [KeyboardFocusObservation] = [
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 43,
            identityToken: "ax:focus",
            bounds: CGRect(x: 150, y: 150, width: 300, height: 200),
            role: role,
            subrole: subrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: container,
            role: role,
            subrole: subrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: CGRect(x: 100, y: 150, width: 300, height: 200),
            role: role,
            subrole: subrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: CGRect(x: CGFloat.infinity, y: 150, width: 300, height: 200),
            role: role,
            subrole: subrole,
            enabled: true,
            containerBounds: container
        ),
    ]
    #expect(rejected.allSatisfy { $0 == .stale })
}

@Test func keyboardFocusCaptureClassifiesSecureAndIndeterminateEvidenceBeforeIdentity() {
    let container = CGRect(x: 100, y: 100, width: 800, height: 600)
    let bounds = CGRect(x: 150, y: 150, width: 300, height: 200)
    let normalRole = BoundedAXStringResult(value: "AXTextArea", status: .complete)
    let normalSubrole = BoundedAXStringResult(value: nil, status: .complete)
    let observations = [
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: bounds,
            role: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
            subrole: normalSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: bounds,
            role: normalRole,
            subrole: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: bounds,
            role: BoundedAXStringResult(value: nil, status: .complete),
            subrole: normalSubrole,
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: bounds,
            role: normalRole,
            subrole: BoundedAXStringResult(value: nil, status: .truncated),
            enabled: true,
            containerBounds: container
        ),
        makeKeyboardFocusObservation(
            expectedPID: 42,
            actualPID: 42,
            identityToken: "ax:focus",
            bounds: bounds,
            role: normalRole,
            subrole: normalSubrole,
            enabled: nil,
            containerBounds: container
        ),
    ]

    #expect(observations.prefix(2).allSatisfy { $0 == .secure })
    #expect(observations.dropFirst(2).allSatisfy { $0 == .secureOrIndeterminate })
}

@Test func dispatcherReturnsTargetWindowSnapshotFromInjectedObserver() {
    let observer = StaticWindows()
    let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
        #"{"protocol_version":4,"request_id":"snapshot-1","operation":"snapshot","payload":{"app_ref":"app_1","window_ref":"win_1","scope":"target_window","artifact_name":"capture.png"}}"#
    )

    #expect(response.ok)
    #expect(response.snapshot != nil)
}

@Test func dispatcherForwardsDisplayScopeToTheObserver() {
    let observer = StaticWindows()
    let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
        #"{"protocol_version":4,"request_id":"scope-1","operation":"snapshot","payload":{"app_ref":"app_1","window_ref":"win_1","scope":"display","artifact_name":"capture.png"}}"#
    )

    #expect(response.ok)
    #expect(observer.snapshotScopes == ["display"])
}

@Test func dispatcherReturnsAtomicGetAppStateResultAndSnapshotWithoutCallingLegacySelectOrSnapshot() {
    let observer = StaticWindows()
    let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
        #"{"protocol_version":4,"request_id":"state-1","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )

    #expect(response.ok)
    #expect(response.error == nil)
    guard case let .object(result)? = response.result else {
        Issue.record("missing get_app_state result")
        return
    }
    if case let .string(appRef)? = result["app_ref"] { #expect(appRef == "app_1") } else { Issue.record("missing app_ref") }
    if case let .string(windowRef)? = result["window_ref"] { #expect(windowRef == "win_1") } else { Issue.record("missing window_ref") }
    if case let .number(generation)? = result["catalog_generation"] { #expect(generation == 7) } else { Issue.record("missing catalog_generation") }
    if case let .string(mode)? = result["interaction_mode"] { #expect(mode == "background") } else { Issue.record("missing interaction_mode") }
    #expect(response.snapshot != nil)
    #expect(observer.getAppStateCalls == 1)
    #expect(observer.selectCount == 0)
    #expect(observer.snapshotScopes.isEmpty)
}

@Test func dispatcherForwardsDisplayScopeToAtomicGetAppState() {
    let observer = StaticWindows()
    let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
        #"{"protocol_version":4,"request_id":"state-display","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"display","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )

    #expect(response.ok)
    #expect(observer.getAppStateScopes == ["display"])
}

@Test func dispatcherGetAppStateMapsBoundedErrorsAndNeverReturnsPartialSuccess() {
    let expected: [(WindowObservationError, String)] = [
        (.staleTarget, "stale_target"),
        (.permissionDenied("missing"), "permission_denied"),
        (.targetGone, "target_gone"),
        (.captureFailed, "snapshot_failed"),
        (.axSerializationFailed, "snapshot_failed"),
        (.capturePublicationUncertain, "snapshot_failed"),
    ]
    for (error, code) in expected {
        let observer = StaticWindows()
        observer.getAppStateError = error
        let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
            #"{"protocol_version":4,"request_id":"state-error","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
        )

        #expect(!response.ok)
        #expect(response.error?.code == code)
        #expect(response.result == nil)
        #expect(response.snapshot == nil)
    }
}

@Test func dispatcherPerformsNoFallibleAppStateValidationAfterAtomicObserverReturns() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Protocol.swift"),
        encoding: .utf8
    )
    let operationStart = try #require(source.range(of: "        case \"get_app_state\":"))
    let operationEnd = try #require(source.range(
        of: "        case \"select\":",
        range: operationStart.upperBound..<source.endIndex
    ))
    let operation = String(source[operationStart.lowerBound..<operationEnd.lowerBound])
    let atomicReturn = try #require(operation.range(of: "windows.getAppState("))
    let afterAtomicReturn = operation[atomicReturn.upperBound...]

    #expect(!afterAtomicReturn.contains("validGetAppStateResponse"))
    #expect(!afterAtomicReturn.contains("guard valid"))
}

@Test func windowObservingSurfaceExcludesLegacyFocusAndAct() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Windows.swift"),
        encoding: .utf8
    )

    let protocolStart = try #require(source.range(of: "protocol WindowObserving {"))
    let protocolEnd = try #require(source.range(of: "\n}\n\nextension WindowObserving", range: protocolStart.upperBound..<source.endIndex))
    let protocolSurface = String(source[protocolStart.lowerBound..<protocolEnd.lowerBound])

    #expect(!protocolSurface.contains("func focus("))
    #expect(!protocolSurface.contains("func act("))
}

@Test func systemWindowObserverForegroundCallerPreservesConsumeActionErrors() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Windows.swift"),
        encoding: .utf8
    )

    let callerStart = try #require(source.range(of: "    private func foregroundCooperativeAct("))
    let callerEnd = try #require(source.range(
        of: "\n    private func executeForegroundActions(",
        range: callerStart.upperBound..<source.endIndex
    ))
    let caller = String(source[callerStart.lowerBound..<callerEnd.lowerBound])
    let typedCatch = try #require(caller.range(of: "catch let error as ActionExecutionError"))
    let genericCatch = try #require(caller.range(of: "catch {", range: typedCatch.upperBound..<caller.endIndex))
    let typedMapping = caller[typedCatch.lowerBound..<genericCatch.lowerBound]

    #expect(typedMapping.contains("error: .action(error)"))
    #expect(typedMapping.contains("lastAcknowledgedAction: -1"))
}

@Test func systemWindowObserverFreshFragmentSnapshotUsesForegroundObservationWithoutRearming() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Windows.swift"),
        encoding: .utf8
    )

    let observerStart = try #require(source.range(of: "final class SystemWindowObserver: WindowObserving {"))
    let snapshotStart = try #require(source.range(
        of: "    func snapshot(\n        appRef: String,\n        windowRef: String,\n        scope: String,\n        artifactName: String?,\n        textDetail: SnapshotTextDetailRequest\n    ) throws -> JSONValue {",
        range: observerStart.upperBound..<source.endIndex
    ))
    let snapshotEnd = try #require(source.range(
        of: "\n    func planActions(",
        range: snapshotStart.upperBound..<source.endIndex
    ))
    let snapshot = String(source[snapshotStart.lowerBound..<snapshotEnd.lowerBound])

    let resolved = try #require(snapshot.range(of: "let selectedTarget = try resolve"))
    let foreground = try #require(snapshot.range(of: "takeoverCoordinator.fragmentObservationTarget(for: selectedTarget)"))
    let overlay = try #require(snapshot.range(of: "containedAppOwnedOverlayActive"))
    let keyboardFocus = try #require(snapshot.range(of: "currentKeyboardFocusObservation"))

    #expect(resolved.lowerBound < foreground.lowerBound)
    #expect(foreground.lowerBound < overlay.lowerBound)
    #expect(overlay.lowerBound < keyboardFocus.lowerBound)
    #expect(!snapshot.contains("activation.activate"))
    #expect(!snapshot.contains("activity.arm"))
}

@Test func smartSnapshotPublishesOnlyAfterFinalIdentityAndFocusAndRegistersOnlyAfterDurability() throws {
    enum SpyFailure: Error { case validation, publication }

    var events: [String] = []
    let authority = try publishAfterFinalSnapshotValidation(
        validate: {
            events.append("validate")
            return 42
        },
        publish: { value in
            #expect(value == 42)
            events.append("publish")
        },
        register: { value in
            #expect(value == 42)
            events.append("register")
        }
    )
    #expect(authority == 42)
    #expect(events == ["validate", "publish", "register"])

    events = []
    #expect(throws: SpyFailure.self) {
        try publishAfterFinalSnapshotValidation(
            validate: {
                events.append("validate")
                throw SpyFailure.validation
            },
            publish: { (_: Int) in events.append("publish") },
            register: { (_: Int) in events.append("register") }
        )
    }
    #expect(events == ["validate"])

    events = []
    #expect(throws: SpyFailure.self) {
        try publishAfterFinalSnapshotValidation(
            validate: {
                events.append("validate")
                return 42
            },
            publish: { _ in
                events.append("publish")
                throw SpyFailure.publication
            },
            register: { (_: Int) in events.append("register") }
        )
    }
    #expect(events == ["validate", "publish"])
}

@Test func appStateProductionTransactionSeamFailsClosedAtEveryStageAndCommitsOnceOnSuccess() throws {
    let faults: [AppStateProductionTransactionFault?] = [
        .pngCapture,
        .axSerialization,
        .responseShape,
        .responseScopeMismatch,
        .secondArtifactWrite,
        .secondArtifactSync,
        .secondArtifactRename,
        .directorySync,
        .finalIdentityDrift,
        .cancellation,
        nil,
    ]
    for fault in faults {
        try withBundleTestDirectory { root, descriptor in
            try runAppStateProductionTransactionFixture(
                fault: fault,
                root: root,
                descriptor: descriptor
            )
        }
    }
}

@Test func systemWindowObserverAcceptsTextDetailAndStillChecksPermissionsBeforeCapture() {
    let permissions = SnapshotPermissionSpy()
    let cursor = SnapshotCursorSpy()
    var invalidations: [SnapshotInvalidationReason] = []
    let observer = SystemWindowObserver(
        permissions: permissions,
        virtualCursor: cursor,
        cooperativeStateInvalidationObserver: { invalidations.append($0) }
    )
    let dispatcher = Dispatcher(permissions: permissions, windows: observer)

    let response = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"unsupported-detail","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png","text_detail":"on","text_detail_artifact_name":"snapshot-0123456789abcdef0123456789abcdef.ax.json"}}"#
    )

    #expect(!response.ok)
    #expect(response.error?.code == "permission_denied")
    #expect(invalidations == [.newSnapshot])
    #expect(permissions.reads == ["screen-recording"])
    #expect(cursor.events == ["hide"])
}

@Test func nativeCatalogFirstSuccessfulRefreshStartsAtGenerationOneAndLaterRefreshesIncrementOnce() throws {
    let activation = CatalogActivationSpy()
    let first = catalogObservation(appRef: "app_first", windowRef: "win_first")
    let second = catalogObservation(appRef: "app_second", windowRef: "win_second")
    var observations = [first, second]
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        activation: activation,
        catalogBuilder: { observations.removeFirst() }
    )

    #expect(catalogGeneration(try observer.apps()) == 1)
    #expect(catalogGeneration(try observer.apps()) == 2)
    #expect(activation.activateCount == 0)
}

@Test func catalogWindowJSONPublishesBoundedExistingFieldsAndExactBindingEligibility() {
    let windowRef = String(repeating: "r", count: maximumAXStringCharacters + 1)
    let title = String(repeating: "t", count: maximumAXStringCharacters + 1)
    let documentPath = String(repeating: "d", count: maximumAXStringCharacters + 1)
    let bounds = CGRect(x: 1, y: 2, width: 300, height: 200)

    let cases: [(Bool, Bool, Bool, String)] = [
        (false, false, false, "accessibility_permission_required"),
        (true, false, false, "ax_window_unmatched"),
        (true, true, true, "ready"),
    ]
    for (accessibilityTrusted, exactAXWindowFound, bindable, status) in cases {
        let values = catalogWindowJSON(
            windowRef: windowRef,
            title: title,
            bounds: bounds,
            documentPath: documentPath,
            accessibilityTrusted: accessibilityTrusted,
            exactAXWindowFound: exactAXWindowFound
        )

        #expect(jsonString(values["window_ref"]) == windowRef)
        #expect(jsonString(values["title"]) == String(title.prefix(maximumAXStringCharacters)))
        #expect(jsonBounds(values["bounds"]) == [
            "x": 1, "y": 2, "width": 300, "height": 200,
        ])
        #expect(jsonString(values["document_path"]) == String(documentPath.prefix(maximumAXStringCharacters)))
        #expect(jsonBool(values["bindable"]) == bindable)
        #expect(jsonString(values["binding_status"]) == status)
        #expect(Set(values.keys) == [
            "window_ref", "title", "bounds", "document_path", "bindable", "binding_status",
        ])
    }
}

@Test func nativeCatalogReusesOnlyExactHelperLifetimeWindowIdentityAndPrunesAbsentWindows() throws {
    let stableAXWindow = AXUIElementCreateApplication(999)
    let rebuiltAXWindow = AXUIElementCreateApplication(998)
    var observedWindows = [
        CatalogSCWindow(
            pid: 999,
            windowID: 42,
            bounds: CGRect(x: 1, y: 2, width: 300, height: 200),
            isOnScreen: true,
            title: "Catalog Window",
            applicationName: "Catalog App",
            bundleID: "dev.astra.catalog",
            appVersion: "1.0",
            isTerminated: false,
            isRegularApplication: true
        ),
    ]
    var matchedAXWindow: AXUIElement? = stableAXWindow
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogWindows: { observedWindows },
        catalogAXWindowMatcher: { _, _ in matchedAXWindow }
    )

    func onlyWindow(_ catalog: JSONValue) throws -> (String, String, [String: JSONValue]) {
        guard case let .object(catalogValues) = catalog,
              case let .array(apps)? = catalogValues["apps"],
              case let .object(app)? = apps.first,
              case let .string(appRef)? = app["app_ref"],
              case let .array(windows)? = app["windows"],
              case let .object(window)? = windows.first,
              case let .string(windowRef)? = window["window_ref"]
        else {
            throw WindowObservationError.targetGone
        }
        return (appRef, windowRef, window)
    }

    let first = try onlyWindow(observer.apps())
    let second = try onlyWindow(observer.apps())
    let firstIdentity = try #require(jsonString(first.2["window_identity_ref"]))
    #expect(!firstIdentity.isEmpty)
    #expect(firstIdentity.count <= maximumAXStringCharacters)
    #expect(jsonString(second.2["window_identity_ref"]) == firstIdentity)
    #expect(second.0 != first.0)
    #expect(second.1 != first.1)

    matchedAXWindow = rebuiltAXWindow
    let rebuilt = try onlyWindow(observer.apps())
    let rebuiltIdentity = try #require(jsonString(rebuilt.2["window_identity_ref"]))
    #expect(rebuiltIdentity != firstIdentity)

    matchedAXWindow = nil
    let unbindable = try onlyWindow(observer.apps())
    #expect(jsonBool(unbindable.2["bindable"]) == false)
    #expect(unbindable.2["window_identity_ref"] == nil)

    observedWindows = []
    _ = try observer.apps()
    matchedAXWindow = stableAXWindow
    observedWindows = [
        CatalogSCWindow(
            pid: 999,
            windowID: 42,
            bounds: CGRect(x: 1, y: 2, width: 300, height: 200),
            isOnScreen: true,
            title: "Catalog Window",
            applicationName: "Catalog App",
            bundleID: "dev.astra.catalog",
            appVersion: "1.0",
            isTerminated: false,
            isRegularApplication: true
        ),
    ]
    let returned = try onlyWindow(observer.apps())
    #expect(jsonString(returned.2["window_identity_ref"]) != firstIdentity)
}

@Test func catalogContinuityHashCollisionNeverReusesUnequalAXWindowIdentity() throws {
    let original = AXUIElementCreateApplication(991)
    let rebuilt = AXUIElementCreateSystemWide()
    var matched = original
    let window = CatalogSCWindow(
        pid: 991,
        windowID: 42,
        bounds: CGRect(x: 1, y: 2, width: 300, height: 200),
        isOnScreen: true,
        title: "Catalog Window",
        applicationName: "Catalog App",
        bundleID: "dev.astra.catalog",
        appVersion: "1.0",
        isTerminated: false,
        isRegularApplication: true
    )
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogWindows: { [window] },
        catalogAXWindowMatcher: { _, _ in matched },
        catalogAXIdentityHash: { _ in 7 }
    )

    let first = try #require(firstCatalogWindowIdentity(observer.apps()))
    matched = rebuilt
    let second = try #require(firstCatalogWindowIdentity(observer.apps()))

    #expect(!CFEqual(original, rebuilt))
    #expect(second != first)
}

@Test func catalogContinuityAmbiguousExactIdentityBucketMintsNewToken() throws {
    let element = AXUIElementCreateApplication(993)
    let firstWindow = CatalogSCWindow(
        pid: 993,
        windowID: 42,
        bounds: CGRect(x: 1, y: 2, width: 300, height: 200),
        isOnScreen: true,
        title: "First",
        applicationName: "Catalog App",
        bundleID: "dev.astra.catalog",
        appVersion: "1.0",
        isTerminated: false,
        isRegularApplication: true
    )
    let secondWindow = CatalogSCWindow(
        pid: 993,
        windowID: 42,
        bounds: firstWindow.bounds,
        isOnScreen: true,
        title: "Second",
        applicationName: firstWindow.applicationName,
        bundleID: firstWindow.bundleID,
        appVersion: firstWindow.appVersion,
        isTerminated: false,
        isRegularApplication: true
    )
    var windows = [firstWindow, secondWindow]
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogWindows: { windows },
        catalogAXWindowMatcher: { _, _ in element },
        catalogAXIdentityHash: { _ in 7 }
    )

    let ambiguousTokens = catalogWindowIdentities(try observer.apps())
    windows = [firstWindow]
    let next = try #require(firstCatalogWindowIdentity(observer.apps()))

    #expect(ambiguousTokens.count == 2)
    #expect(Set(ambiguousTokens).count == 2)
    #expect(!ambiguousTokens.contains(next))
}

@Test func nativeCatalogBuildPipelinePublishesBindingEligibilityAndRetainsTheExactAXWindow() throws {
    let expectedAXWindow = AXUIElementCreateApplication(999)
    var matchedTargets: [WindowTarget] = []
    var matcherCalls: [CGWindowID: Int] = [:]
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogWindows: {
            [
                CatalogSCWindow(
                    pid: 999,
                    windowID: 42,
                    bounds: CGRect(x: 1, y: 2, width: 300, height: 200),
                    isOnScreen: true,
                    title: "Catalog Window",
                    applicationName: "Catalog App",
                    bundleID: "dev.astra.catalog",
                    appVersion: "1.0",
                    isTerminated: false,
                    isRegularApplication: true
                ),
                CatalogSCWindow(
                    pid: 999,
                    windowID: 43,
                    bounds: CGRect(x: 400, y: 2, width: 300, height: 200),
                    isOnScreen: true,
                    title: "Unmatched Window",
                    applicationName: "Catalog App",
                    bundleID: "dev.astra.catalog",
                    appVersion: "1.0",
                    isTerminated: false,
                    isRegularApplication: true
                ),
            ]
        },
        catalogAXWindowMatcher: { _, target in
            matchedTargets.append(target)
            matcherCalls[target.windowID, default: 0] += 1
            return target.windowID == 42 ? expectedAXWindow : nil
        }
    )

    let catalog = try observer.apps()
    guard case let .object(catalogValues) = catalog,
          case let .array(apps)? = catalogValues["apps"],
          case let .object(app)? = apps.first,
          case let .array(windows)? = app["windows"],
          case let .string(appRef)? = app["app_ref"]
    else {
        Issue.record("catalog did not contain the injected window")
        return
    }
    let catalogWindows = windows.compactMap { value -> [String: JSONValue]? in
        guard case let .object(window) = value else { return nil }
        return window
    }
    guard let matchedWindow = catalogWindows.first(where: {
        jsonString($0["title"]) == "Catalog Window"
    }), let unmatchedWindow = catalogWindows.first(where: {
        jsonString($0["title"]) == "Unmatched Window"
    }), let windowRef = jsonString(matchedWindow["window_ref"])
    else {
        Issue.record("catalog did not preserve both injected windows")
        return
    }

    #expect(catalogWindows.count == 2)
    #expect(jsonBool(matchedWindow["bindable"]) == true)
    #expect(jsonString(matchedWindow["binding_status"]) == "ready")
    #expect(jsonBool(unmatchedWindow["bindable"]) == false)
    #expect(jsonString(unmatchedWindow["binding_status"]) == "ax_window_unmatched")
    #expect(matchedTargets.map(\.windowID) == [42, 43])
    #expect(matcherCalls == [42: 1, 43: 1])
    let retained = try observer.resolveCatalogTarget(
        catalogGeneration: 1,
        appRef: appRef,
        windowRef: windowRef,
        current: { $0 }
    )
    #expect(retained.axIdentity == CFHash(expectedAXWindow))
    #expect(retained.axElement.map { CFEqual($0, expectedAXWindow) } == true)
}

@Test func injectedCatalogAXMatcherNilDoesNotFallBackToSystemMatching() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Windows.swift"),
        encoding: .utf8
    )
    let start = try #require(source.range(of: "    private func catalogExactAXWindow("))
    let end = try #require(source.range(
        of: "\n    private func waitForShareableWindows()",
        range: start.upperBound..<source.endIndex
    ))
    let implementation = String(source[start.lowerBound..<end.lowerBound])

    #expect(implementation.contains("if let catalogAXWindowMatcher {"))
    #expect(implementation.contains("return catalogAXWindowMatcher(app, target)"))
    #expect(!implementation.contains("?? matchingAXWindow"))
}

@Test func nativeCatalogFailedRefreshPreservesThePriorRegistryAndGeneration() throws {
    enum ExpectedFailure: Error { case refresh }

    let first = catalogObservation(appRef: "app_first", windowRef: "win_first")
    let second = catalogObservation(appRef: "app_second", windowRef: "win_second")
    var attempts = 0
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogBuilder: {
            attempts += 1
            switch attempts {
            case 1: return first
            case 2: throw ExpectedFailure.refresh
            default: return second
            }
        }
    )

    #expect(catalogGeneration(try observer.apps()) == 1)
    #expect(throws: ExpectedFailure.self) { try observer.apps() }
    let retained = try observer.resolveCatalogTarget(
        catalogGeneration: 1,
        appRef: "app_first",
        windowRef: "win_first",
        current: { $0 }
    )
    #expect(retained.windowRef == "win_first")
    #expect(catalogGeneration(try observer.apps()) == 2)
}

@Test func nativeCatalogRefreshInvalidatesOldOpaqueRefsEvenWhenTheWindowIdentityMatches() throws {
    let first = catalogObservation(appRef: "app_first", windowRef: "win_first")
    let second = catalogObservation(appRef: "app_second", windowRef: "win_second")
    var observations = [first, second]
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogBuilder: { observations.removeFirst() }
    )

    _ = try observer.apps()
    _ = try observer.apps()

    for generation in [1, 2] {
        do {
            _ = try observer.resolveCatalogTarget(
                catalogGeneration: generation,
                appRef: "app_first",
                windowRef: "win_first",
                current: { $0 }
            )
            Issue.record("old catalog refs must be rejected")
        } catch WindowObservationError.staleTarget {
        } catch {
            Issue.record("expected staleTarget, got \(error)")
        }
    }
}

@Test func nativeCatalogLookupDistinguishesGoneTargetsFromCurrentIdentityDrift() throws {
    let observation = catalogObservation(appRef: "app", windowRef: "win")
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        catalogBuilder: { observation }
    )
    _ = try observer.apps()

    do {
        _ = try observer.resolveCatalogTarget(
            catalogGeneration: 1,
            appRef: "app",
            windowRef: "win",
            current: { _ in nil }
        )
        Issue.record("disappeared target must be targetGone")
    } catch WindowObservationError.targetGone {
    } catch {
        Issue.record("expected targetGone, got \(error)")
    }
    for changed in [
        catalogTarget(appRef: "app", windowRef: "win", pid: 8),
        catalogTarget(appRef: "app", windowRef: "win", windowID: 8),
        catalogTarget(appRef: "app", windowRef: "win", bounds: CGRect(x: 2, y: 2, width: 300, height: 200)),
        catalogTarget(appRef: "app", windowRef: "win", axIdentity: 8),
    ] {
        do {
            _ = try observer.resolveCatalogTarget(
                catalogGeneration: 1,
                appRef: "app",
                windowRef: "win",
                current: { _ in changed }
            )
            Issue.record("changed current record must be stale")
        } catch WindowObservationError.staleTarget {
        } catch {
            Issue.record("expected staleTarget, got \(error)")
        }
    }
}

@Test func nativeCatalogGenerationIsAlwaysAPositiveExactJavaScriptInteger() throws {
    #expect(try nextCatalogGeneration(after: 0) == 1)
    #expect(try nextCatalogGeneration(after: maximumCatalogGeneration - 1) == maximumCatalogGeneration)
    #expect(throws: CatalogGenerationError.self) { try nextCatalogGeneration(after: -1) }
    #expect(throws: CatalogGenerationError.self) {
        try nextCatalogGeneration(after: maximumCatalogGeneration)
    }
}

@Test func nativeGetAppStateRejectsStaleCatalogBindingBeforeAnyLiveSelection() throws {
    let activation = CatalogActivationSpy()
    let observation = catalogObservation(appRef: "app", windowRef: "win")
    let observer = SystemWindowObserver(
        permissions: StaticPermissions(),
        activation: activation,
        catalogBuilder: { observation }
    )
    _ = try observer.apps()

    do {
        _ = try observer.getAppState(
            appRef: "app",
            windowRef: "win",
            catalogGeneration: 2,
            scope: "target_window",
            artifactName: "snapshot-0123456789abcdef0123456789abcdef.png",
            textDetail: .off
        )
        Issue.record("stale generation must fail before live selection")
    } catch WindowObservationError.staleTarget {
    } catch {
        Issue.record("expected staleTarget, got \(error)")
    }
    #expect(activation.activateCount == 0)
}

@Test func systemWindowObserverPrunesFragmentDraftAndDispatcherAuthorityTogether() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let source = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/AstraMacComputerHelper/Windows.swift"),
        encoding: .utf8
    )

    let observerStart = try #require(source.range(of: "final class SystemWindowObserver: WindowObserving {"))
    let observerEnd = try #require(source.range(
        of: "\n}\n\nprivate struct StoredCooperativePlan",
        range: observerStart.upperBound..<source.endIndex
    ))
    let observer = String(source[observerStart.lowerBound..<observerEnd.lowerBound])

    #expect(observer.contains("fragmentDraftClock: cooperativePlanClock"))
    #expect(observer.contains("pruneCooperativeFragmentDraftsLocked(now: planTime)"))
    #expect(observer.contains("reserveCooperativeFragmentDraftCapacityLocked()"))
    #expect(observer.contains("removeValue(forKey: reference)?.dispatcher.invalidatePlans()"))
    #expect(observer.contains("InputDispatcher.maximumFragmentDrafts"))
}

@Test func dispatcherSelectUsesBackgroundModeWithoutCallingLegacyFocus() {
    let observer = StaticWindows()
    let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
        #"{"protocol_version":4,"request_id":"select-1","operation":"select","payload":{"app_ref":"app_1","window_ref":"win_1"}}"#
    )

    #expect(response.ok)
    #expect(observer.selectCount == 1)
    guard case let .object(result)? = response.result else {
        Issue.record("select result must be an object")
        return
    }
    guard case let .string(mode)? = result["interaction_mode"] else {
        Issue.record("select result must include interaction_mode")
        return
    }
    #expect(mode == "background")
}

@Test func actionGuardRetainsTheTargetInteractionMode() {
    let guardValue = ActionGuard(
        pid: 1,
        windowID: 2,
        bounds: CGRect(x: 0, y: 0, width: 10, height: 10),
        axIdentity: 3,
        snapshotID: "snapshot",
        interactionMode: .background
    )

    #expect(guardValue.interactionMode == .background)
}

@Test func containedOverlayTraversalFailsClosedWhenChildrenAreIndeterminate() {
    let root = AXUIElementCreateApplication(42)
    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: { _, _ in nil },
        attributes: { _ in nil }
    ))
}

@Test func deepChildrenReadFailureSkipsTheBranchInsteadOfFailingTheWindow() {
    let root = AXUIElementCreateApplication(42)
    let shallow = AXUIElementCreateApplication(43)
    let chain1 = AXUIElementCreateApplication(44)
    let chain2 = AXUIElementCreateApplication(45)
    let chain3 = AXUIElementCreateApplication(46)
    let chain4 = AXUIElementCreateApplication(47)
    let chain5 = AXUIElementCreateApplication(48)

    // Mirrors the live Finder failure: a deep content subtree (depth 5)
    // fails its children read while a normal shallow branch coexists.
    let children: (AXUIElement, Int) -> [AXUIElement]? = { element, _ in
        if CFEqual(element, root) { return [shallow, chain1] }
        if CFEqual(element, shallow) { return [] }
        if CFEqual(element, chain1) { return [chain2] }
        if CFEqual(element, chain2) { return [chain3] }
        if CFEqual(element, chain3) { return [chain4] }
        if CFEqual(element, chain4) { return [chain5] }
        if CFEqual(element, chain5) { return nil }
        return []
    }
    let group = AXOverlayAttributes(
        role: BoundedAXStringResult(value: "AXGroup", status: .complete),
        subrole: BoundedAXStringResult(value: nil, status: .complete),
        bounds: CGRect(x: 0, y: 0, width: 800, height: 600)
    )

    #expect(!containedAXOverlayOrUncertain(
        root: root,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: children,
        attributes: { _ in group }
    ))
}

@Test func shallowChildrenReadFailureStillFailsClosed() {
    let root = AXUIElementCreateApplication(42)
    let shallow = AXUIElementCreateApplication(43)

    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: { element, _ in
            if CFEqual(element, root) { return [shallow] }
            if CFEqual(element, shallow) { return nil }
            return []
        },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXGroup", status: .complete),
                subrole: BoundedAXStringResult(value: nil, status: .complete),
                bounds: nil
            )
        }
    ))
}

@Test func scanBudgetExhaustionBelowTheShallowLayerIsNotUncertain() {
    let root = AXUIElementCreateApplication(42)
    // Synthetic tree: every node fans out to 8 children, so breadth-first
    // scanning exhausts the 128-node budget only after the shallow layer
    // (depth <= 2) has been fully checked — exactly the Finder shape.
    var nextPID: Int32 = 100
    let children: (AXUIElement, Int) -> [AXUIElement]? = { _, _ in
        var produced: [AXUIElement] = []
        for _ in 0..<8 {
            produced.append(AXUIElementCreateApplication(nextPID))
            nextPID += 1
        }
        return produced
    }
    let group = AXOverlayAttributes(
        role: BoundedAXStringResult(value: "AXGroup", status: .complete),
        subrole: BoundedAXStringResult(value: nil, status: .complete),
        bounds: CGRect(x: 0, y: 0, width: 800, height: 600)
    )

    #expect(!containedAXOverlayOrUncertain(
        root: root,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: children,
        attributes: { _ in group }
    ))
}

@Test func sharedChildReferenceIsSkippedInsteadOfFlaggingOverlay() {
    let root = AXUIElementCreateApplication(42)
    let first = AXUIElementCreateApplication(43)
    let shared = AXUIElementCreateApplication(44)

    // Office-style trees legitimately reference the same element from two
    // parents. The visited set must skip the duplicate — revisiting is not
    // occlusion evidence, and the budget already bounds the traversal.
    let children: (AXUIElement, Int) -> [AXUIElement]? = { element, _ in
        if CFEqual(element, root) { return [first, shared] }
        if CFEqual(element, first) { return [shared] }
        return []
    }
    let group = AXOverlayAttributes(
        role: BoundedAXStringResult(value: "AXGroup", status: .complete),
        subrole: BoundedAXStringResult(value: nil, status: .complete),
        bounds: CGRect(x: 0, y: 0, width: 800, height: 600)
    )

    #expect(!containedAXOverlayOrUncertain(
        root: root,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: children,
        attributes: { _ in group }
    ))
}

@Test func containedOverlayTraversalFindsNestedSheet() {
    let root = AXUIElementCreateApplication(42)
    let group = AXUIElementCreateApplication(43)
    let sheet = AXUIElementCreateApplication(44)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: targetBounds,
        children: { element, _ in
            if CFEqual(element, root) { return [group] }
            if CFEqual(element, group) { return [sheet] }
            return []
        },
        attributes: { element in
            AXOverlayAttributes(
                role: BoundedAXStringResult(
                    value: CFEqual(element, sheet) ? "AXSheet" : "AXGroup",
                    status: .complete
                ),
                subrole: BoundedAXStringResult(value: nil, status: .complete),
                bounds: CFEqual(element, sheet)
                    ? CGRect(x: 100, y: 100, width: 400, height: 300)
                    : targetBounds
            )
        }
    ))
}

@Test func containedOverlayTraversalRejectsEqualBoundsSheetDistinctFromSelectedElement() {
    let root = AXUIElementCreateApplication(42)
    let sheet = AXUIElementCreateApplication(43)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: targetBounds,
        children: { element, _ in CFEqual(element, root) ? [sheet] : [] },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXSheet", status: .complete),
                subrole: BoundedAXStringResult(value: "AXDialog", status: .complete),
                bounds: targetBounds
            )
        }
    ))
}

@Test func containedOverlayTraversalFailsClosedForIncompleteRoleEvidence() {
    let root = AXUIElementCreateApplication(42)
    let candidate = AXUIElementCreateApplication(43)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    for role in [
        BoundedAXStringResult(value: "AXGro", status: .truncated),
        BoundedAXStringResult(value: nil, status: .complete),
    ] {
        #expect(containedAXOverlayOrUncertain(
            root: root,
            targetBounds: targetBounds,
            children: { element, _ in CFEqual(element, root) ? [candidate] : [] },
            attributes: { _ in
                AXOverlayAttributes(
                    role: role,
                    subrole: BoundedAXStringResult(value: nil, status: .complete),
                    bounds: targetBounds
                )
            }
        ))
    }

    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: targetBounds,
        children: { element, _ in CFEqual(element, root) ? [candidate] : [] },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXGroup", status: .complete),
                subrole: BoundedAXStringResult(value: "AXDia", status: .truncated),
                bounds: targetBounds
            )
        }
    ) == false)
}

@Test func containedOverlayTraversalIgnoresFailedSubroleForOrdinaryElements() {
    let root = AXUIElementCreateApplication(42)
    let group = AXUIElementCreateApplication(43)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    // Chromium/Electron commonly fails subrole reads for ordinary AXGroup
    // descendants; a failed subrole read is not occlusion evidence.
    #expect(!containedAXOverlayOrUncertain(
        root: root,
        targetBounds: targetBounds,
        children: { element, _ in CFEqual(element, root) ? [group] : [] },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXGroup", status: .complete),
                subrole: BoundedAXStringResult(value: nil, status: .failed),
                bounds: targetBounds
            )
        }
    ))
}

@Test func containedOverlayTraversalStillDetectsSheetWithFailedSubrole() {
    let root = AXUIElementCreateApplication(42)
    let sheet = AXUIElementCreateApplication(43)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    // Role-level AXSheet evidence still detects an overlay even when the
    // subrole attribute is unavailable.
    #expect(containedAXOverlayOrUncertain(
        root: root,
        targetBounds: targetBounds,
        children: { element, _ in CFEqual(element, root) ? [sheet] : [] },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXSheet", status: .complete),
                subrole: BoundedAXStringResult(value: nil, status: .failed),
                bounds: CGRect(x: 100, y: 100, width: 400, height: 300)
            )
        }
    ))
}

@Test func containedOverlayTraversalFailsClosedWhenTraversalBudgetIsExhausted() {
    let elements = (42...171).map(AXUIElementCreateApplication)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    // 129 shallow children exceed the 128-node budget on the first level, so
    // the traversal is still indeterminate (fail closed).
    #expect(containedAXOverlayOrUncertain(
        root: elements[0],
        targetBounds: targetBounds,
        children: { element, _ in
            CFEqual(element, elements[0]) ? Array(elements[1...]) : []
        },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXGroup", status: .complete),
                subrole: BoundedAXStringResult(value: nil, status: .complete),
                bounds: targetBounds
            )
        }
    ))
}

@Test func containedOverlayTraversalStopsAtDepthLimitWithoutUncertainty() {
    let elements = (42...117).map(AXUIElementCreateApplication)
    let targetBounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    // A deep chain (well beyond the depth limit) with ordinary groups must
    // not make the window uncertain: window-level overlays are shallow AX
    // descendants, and deep content trees cannot contain them.
    #expect(!containedAXOverlayOrUncertain(
        root: elements[0],
        targetBounds: targetBounds,
        children: { element, _ in
            guard let index = elements.firstIndex(where: { CFEqual($0, element) }),
                  index + 1 < elements.count
            else { return [] }
            return [elements[index + 1]]
        },
        attributes: { _ in
            AXOverlayAttributes(
                role: BoundedAXStringResult(value: "AXGroup", status: .complete),
                subrole: BoundedAXStringResult(value: nil, status: .failed),
                bounds: targetBounds
            )
        }
    ))
}

@Test func exactAXElementEnumerationRejectsUnsupportedAndTruncatedCounts() {
    #expect(completeAXElementCount(
        error: .attributeUnsupported,
        available: 0,
        maximum: 64
    ) == nil)
    #expect(completeAXElementCount(error: .noValue, available: 0, maximum: 64) == nil)
    #expect(completeAXElementCount(error: .success, available: 65, maximum: 64) == nil)
    #expect(completeAXElementCount(error: .success, available: 2, maximum: 64) == 2)
}

@Test func exactAXChildEnumerationRecognizesOnlyConfirmedLeafControls() {
    let buttonRole = BoundedAXStringResult(value: "AXButton", status: .complete)
    let groupRole = BoundedAXStringResult(value: "AXGroup", status: .complete)
    let staticTextRole = BoundedAXStringResult(value: "AXStaticText", status: .complete)
    let noSubrole = BoundedAXStringResult(value: nil, status: .complete)

    #expect(knownAXLeafRole(role: buttonRole, subrole: noSubrole))
    #expect(!knownAXLeafRole(role: groupRole, subrole: noSubrole))
    #expect(!knownAXLeafRole(role: staticTextRole, subrole: noSubrole))
    #expect(completeAXElementCount(
        error: .attributeUnsupported,
        available: 0,
        maximum: 64,
        unsupportedMeansEmpty: true
    ) == 0)
    #expect(completeAXElementCount(
        error: .attributeUnsupported,
        available: 0,
        maximum: 64,
        unsupportedMeansEmpty: false
    ) == nil)
}

@Test func exactAXElementMappingRejectsAnyDroppedRecord() {
    let first = AXUIElementCreateApplication(42)
    let second = AXUIElementCreateApplication(43)

    let records: [Int]? = mapCompleteAXElements([first, second]) { element in
        CFEqual(element, first) ? 1 : nil
    }

    #expect(records == nil)
}

// Live Edge 152 (2026-09-23): a hovered link's address appeared in a thin strip on the window's
// bottom edge (24 pt tall, then 1283 pt wide, alpha animating). It blocked every observation and
// the pre-action check, so no Edge action could run while the pointer rested on a link.
@Test func appStatusStripOnTheBottomEdgeIsNotABlockingOverlay() {
    let window = CGRect(x: 25, y: 30, width: 1319, height: 768)
    func strip(_ bounds: CGRect, layer: Int = 0, pid: pid_t = 42) -> VisibleWindowRecord {
        VisibleWindowRecord(pid: pid, windowID: 9, bounds: bounds, layer: layer, alpha: 0.55, zOrder: 14)
    }
    let narrow = strip(CGRect(x: 28, y: 771, width: 437, height: 24))
    let wide = strip(CGRect(x: 28, y: 771, width: 1283, height: 24))
    #expect(appStatusStripOverlay(narrow, targetBounds: window, targetLayer: 0))
    #expect(appStatusStripOverlay(wide, targetBounds: window, targetLayer: 0))
    // Taller bars, strips away from the bottom edge, other layers and strips leaving the window block.
    #expect(!appStatusStripOverlay(strip(CGRect(x: 28, y: 745, width: 437, height: 50)), targetBounds: window, targetLayer: 0))
    #expect(!appStatusStripOverlay(strip(CGRect(x: 28, y: 60, width: 437, height: 24)), targetBounds: window, targetLayer: 0))
    #expect(!appStatusStripOverlay(strip(CGRect(x: 28, y: 740, width: 437, height: 24)), targetBounds: window, targetLayer: 0))
    #expect(!appStatusStripOverlay(strip(CGRect(x: 28, y: 771, width: 437, height: 24), layer: 3),
                                   targetBounds: window, targetLayer: 0))
    #expect(!appStatusStripOverlay(strip(CGRect(x: 10, y: 771, width: 437, height: 24)), targetBounds: window, targetLayer: 0))

    let target = VisibleWindowRecord(pid: 42, windowID: 273, bounds: window, layer: 0, alpha: 1, zOrder: 15)
    let dropdown = VisibleWindowRecord(pid: 42, windowID: 10, bounds: CGRect(x: 89, y: 70, width: 1038, height: 199),
        layer: 0, alpha: 1, zOrder: 13)
    let withStrip = overlayCandidateRecords([narrow, target], targetPID: 42, targetWindowID: 273, targetBounds: window)
    #expect(withStrip?.map(\.windowID) == [273])
    #expect(!containedVisibleOverlayOrUncertain(targetPID: 42, targetWindowID: 273, targetBounds: window,
                                                records: withStrip))
    // A same-app dropdown still blocks, and another app's strip is not this app's overlay to waive.
    let withDropdown = overlayCandidateRecords([wide, dropdown, target], targetPID: 42, targetWindowID: 273,
                                               targetBounds: window)
    #expect(withDropdown?.map(\.windowID) == [10, 273])
    #expect(containedVisibleOverlayOrUncertain(targetPID: 42, targetWindowID: 273, targetBounds: window,
                                               records: withDropdown))
    #expect(overlayCandidateRecords([strip(narrow.bounds, pid: 77), target], targetPID: 42, targetWindowID: 273,
                                    targetBounds: window)?.count == 2)
    #expect(overlayCandidateRecords(nil, targetPID: 42, targetWindowID: 273, targetBounds: window) == nil)
    #expect(appStatusStripRegions([narrow, dropdown, target], targetPID: 42, targetWindowID: 273,
                                  targetBounds: window) == [narrow.bounds])
}

// Live Edge 152 (2026-09-23): clicking or typing in the address field opens its suggestion list, a
// separate same-layer window that encloses the field (149,78 950x24) and drops 199 or 471 pt below.
// It blocked every observation of the page, so the field could not be typed into or submitted.
@Test func focusedFieldSuggestionListIsNotABlockingOverlay() {
    let window = CGRect(x: 25, y: 30, width: 1319, height: 768)
    let field = CGRect(x: 149, y: 78, width: 950, height: 24)
    func popup(_ bounds: CGRect, layer: Int = 0, pid: pid_t = 42) -> VisibleWindowRecord {
        VisibleWindowRecord(pid: pid, windowID: 10, bounds: bounds, layer: layer, alpha: 1, zOrder: 15)
    }
    let typed = popup(CGRect(x: 89, y: 70, width: 1038, height: 199))
    let zeroSuggest = popup(CGRect(x: 89, y: 70, width: 1038, height: 471))
    #expect(attachedSuggestionPopup(typed, focusedField: field, targetBounds: window, targetLayer: 0))
    #expect(attachedSuggestionPopup(zeroSuggest, focusedField: field, targetBounds: window, targetLayer: 0))
    // AppKit drops the list just under a search field; floating autofill lists hang the same way.
    let search = CGRect(x: 740, y: 42, width: 200, height: 22)
    let finder = CGRect(x: 0, y: 25, width: 1000, height: 600)
    let list = CGRect(x: 715, y: 69, width: 240, height: 118)
    #expect(attachedSuggestionPopup(popup(list), focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(attachedSuggestionPopup(popup(list, layer: 3), focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(attachedSuggestionPopup(popup(CGRect(x: 715, y: -80, width: 240, height: 118)),
                                    focusedField: search, targetBounds: finder, targetLayer: 0))
    // Menus and modal panels take the keyboard, so their layers keep blocking; so do windows that
    // do not hang from the field.
    #expect(!attachedSuggestionPopup(popup(list, layer: 8), focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(!attachedSuggestionPopup(popup(list, layer: 101), focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(!attachedSuggestionPopup(popup(CGRect(x: 715, y: 170, width: 240, height: 118)),
                                     focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(!attachedSuggestionPopup(popup(CGRect(x: 100, y: 69, width: 240, height: 118)),
                                     focusedField: search, targetBounds: finder, targetLayer: 0))
    #expect(!attachedSuggestionPopup(typed, focusedField: CGRect(x: 149, y: 900, width: 950, height: 24),
                                     targetBounds: window, targetLayer: 0))
    // A second document window that happens to start at the field is not a list.
    #expect(!attachedSuggestionPopup(popup(CGRect(x: 89, y: 70, width: 1200, height: 700)),
                                     focusedField: field, targetBounds: window, targetLayer: 0))
    #expect(!attachedSuggestionPopup(popup(CGRect(x: 20, y: 70, width: 1330, height: 300)),
                                     focusedField: field, targetBounds: window, targetLayer: 0))

    let target = VisibleWindowRecord(pid: 42, windowID: 273, bounds: window, layer: 0, alpha: 1, zOrder: 16)
    let waived = overlayCandidateRecords([typed, target], targetPID: 42, targetWindowID: 273, targetBounds: window,
                                         focusedField: field)
    #expect(waived?.map(\.windowID) == [273])
    #expect(!containedVisibleOverlayOrUncertain(targetPID: 42, targetWindowID: 273, targetBounds: window,
                                                records: waived))
    // Without a focused field in the target, or for another app's window, the list still blocks.
    #expect(overlayCandidateRecords([typed, target], targetPID: 42, targetWindowID: 273, targetBounds: window)?
        .map(\.windowID) == [10, 273])
    #expect(overlayCandidateRecords([popup(typed.bounds, pid: 77), target], targetPID: 42, targetWindowID: 273,
                                    targetBounds: window, focusedField: field)?.count == 2)
    #expect(mayHaveSuggestionPopup([typed, target], targetPID: 42, targetWindowID: 273))
    #expect(!mayHaveSuggestionPopup([popup(typed.bounds, pid: 77), target], targetPID: 42, targetWindowID: 273))
    // The waiver is judged against the target's own live record.
    #expect(attachedSuggestionPopupRecords([typed], targetPID: 42, targetWindowID: 273, targetBounds: window,
                                           focusedField: field).isEmpty)
    #expect(attachedSuggestionPopupRecords([typed, target], targetPID: 42, targetWindowID: 273, targetBounds: window,
                                           focusedField: field).map(\.windowID) == [10])
}

@Test func visibleOverlayInventoryFailsClosedWhenUnavailable() {
    #expect(containedVisibleOverlayOrUncertain(
        targetPID: 42,
        targetWindowID: 24,
        targetBounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        records: nil
    ))
}

@Test func backgroundVisibleWindowAmbiguityRejectsEqualBoundsSiblingWithDifferentWindowID() {
    let bounds = CGRect(x: 0, y: 0, width: 800, height: 600)

    #expect(backgroundVisibleWindowAmbiguityActive(
        targetPID: 42,
        targetWindowID: 24,
        targetBounds: bounds,
        records: [
            VisibleWindowRecord(
                pid: 42, windowID: 25, bounds: bounds, layer: 0, alpha: 1, zOrder: 0
            ),
            VisibleWindowRecord(
                pid: 42, windowID: 24, bounds: bounds, layer: 0, alpha: 1, zOrder: 1
            ),
        ]
    ))
}

@Test func backgroundSiblingBehindDoesNotObscureTargetButUnknownOrderingDoes() {
    let bounds = CGRect(x: 0, y: 0, width: 800, height: 600)
    for candidateOrder in [0, 1, 2, Int.max] {
        let records = [
            VisibleWindowRecord(pid: 42, windowID: 24, bounds: bounds, layer: 0, alpha: 1, zOrder: 1),
            VisibleWindowRecord(pid: 42, windowID: 25, bounds: bounds, layer: 0, alpha: 1, zOrder: candidateOrder),
        ]
        #expect(backgroundVisibleWindowAmbiguityActive(targetPID: 42, targetWindowID: 24,
            targetBounds: bounds, records: records) == (candidateOrder != 2))
    }
    #expect(!windowIsProvablyBehind(candidateLayer: nil, candidateOrder: 2, selectedLayer: 0, selectedOrder: 1))
    #expect(!windowIsProvablyBehind(candidateLayer: 0, candidateOrder: 2, selectedLayer: 0, selectedOrder: nil))
    #expect(backgroundVisibleWindowAmbiguityActive(targetPID: 42, targetWindowID: 24,
        targetBounds: bounds, records: [
            VisibleWindowRecord(pid: 42, windowID: 24, bounds: bounds.offsetBy(dx: 100, dy: 0),
                layer: 0, alpha: 1, zOrder: 0),
            VisibleWindowRecord(pid: 42, windowID: 25, bounds: bounds, layer: 0, alpha: 1, zOrder: 1),
        ]))
}

@Test func foregroundEqualBoundsSiblingBehindPreservesSelectedWindowObservation() {
    let bounds = CGRect(x: 0, y: 0, width: 800, height: 600)
    let overlayActive = containedAppOwnedOverlayActive(
        targetPID: 42,
        targetWindowID: 24,
        targetBounds: bounds,
        records: [
            VisibleWindowRecord(
                pid: 42, windowID: 24, bounds: bounds, layer: 0, alpha: 1, zOrder: 0
            ),
            VisibleWindowRecord(
                pid: 42, windowID: 25, bounds: bounds, layer: 0, alpha: 1, zOrder: 1
            ),
        ]
    )
    let preference: FocusedRootPreference = overlayActive ? .containedOverlay : .selectedWindow

    #expect(!overlayActive)
    #expect(preference == .selectedWindow)
    #expect(focusedObservationMaximumDepth(preference: preference) == maximumAXDepth)
}

@Test(arguments: ["behind", "front", "equal", "front_layer", "unknown_order",
    "missing_selected", "duplicate_selected", "moved_selected"])
func foregroundContainedSiblingRequiresProvenOrdering(reason: String) {
    let bounds = CGRect(x: 0, y: 33, width: 1512, height: 859)
    let selected = VisibleWindowRecord(pid: 42, windowID: 24,
        bounds: reason == "moved_selected" ? bounds.offsetBy(dx: 100, dy: 0) : bounds,
        layer: 0, alpha: 1, zOrder: 1)
    let order = reason == "unknown_order" ? Int.max : (reason == "front" ? 0 : (reason == "equal" ? 1 : 2))
    var records = [VisibleWindowRecord(pid: 42, windowID: 25,
        bounds: CGRect(x: 10, y: 44, width: 66, height: 20),
        layer: reason == "front_layer" ? 8 : 0, alpha: 1, zOrder: order)]
    if reason != "missing_selected" { records.append(selected) }
    if reason == "duplicate_selected" { records.append(selected) }
    let active = containedAppOwnedOverlayActive(targetPID: 42, targetWindowID: 24,
        targetBounds: bounds, records: records)
    #expect(active == (reason != "behind"))
    if !active {
        let preference: FocusedRootPreference = .selectedWindow
        #expect(focusedObservationMaximumDepth(preference: preference) == maximumAXDepth)
    }
}

@Test func displayCaptureSelectionRequiresOneContainingBoundedDisplay() throws {
    let target = CGRect(x: 100, y: 100, width: 600, height: 400)
    let selected = try selectDisplayCapture(
        targetBounds: target,
        candidates: [
            DisplayCaptureCandidate(
                displayID: 7,
                bounds: CGRect(x: 0, y: 0, width: 1_920, height: 1_080),
                pixelSize: CGSize(width: 3_840, height: 2_160)
            ),
            DisplayCaptureCandidate(
                displayID: 8,
                bounds: CGRect(x: 1_920, y: 0, width: 1_920, height: 1_080),
                pixelSize: CGSize(width: 1_920, height: 1_080)
            ),
        ]
    )

    #expect(selected.displayID == 7)
    #expect(selected.bounds == CGRect(x: 0, y: 0, width: 1_920, height: 1_080))
}

@Test func displayCaptureSelectionRejectsSpanningUnknownAndOversizedDisplays() {
    let left = DisplayCaptureCandidate(
        displayID: 1,
        bounds: CGRect(x: 0, y: 0, width: 1_000, height: 800),
        pixelSize: CGSize(width: 1_000, height: 800)
    )
    let right = DisplayCaptureCandidate(
        displayID: 2,
        bounds: CGRect(x: 1_000, y: 0, width: 1_000, height: 800),
        pixelSize: CGSize(width: 1_000, height: 800)
    )
    let oversized = DisplayCaptureCandidate(
        displayID: 3,
        bounds: CGRect(x: 0, y: 0, width: 20_000, height: 800),
        pixelSize: CGSize(width: 20_000, height: 800)
    )

    #expect(throws: WindowObservationError.self) {
        try selectDisplayCapture(
            targetBounds: CGRect(x: 900, y: 100, width: 200, height: 300),
            candidates: [left, right]
        )
    }
    #expect(throws: WindowObservationError.self) {
        try selectDisplayCapture(
            targetBounds: CGRect(x: 3_000, y: 100, width: 200, height: 300),
            candidates: [left, right]
        )
    }
    #expect(throws: WindowObservationError.self) {
        try selectDisplayCapture(
            targetBounds: CGRect(x: 100, y: 100, width: 200, height: 300),
            candidates: [oversized]
        )
    }
}

private struct StaticPermissions: PermissionStatusProviding {
    var accessibilityTrusted: Bool { true }
    var screenRecordingAllowed: Bool { true }
}

private final class SnapshotPermissionSpy: PermissionStatusProviding {
    private(set) var reads: [String] = []

    var accessibilityTrusted: Bool {
        reads.append("accessibility")
        return false
    }

    var screenRecordingAllowed: Bool {
        reads.append("screen-recording")
        return false
    }
}

private final class SnapshotCursorSpy: VirtualCursorPresenting {
    private(set) var events: [String] = []

    var sidecarWindowID: UInt32? {
        events.append("sidecar-window")
        return nil
    }

    var windowAuthority: VirtualCursorWindowAuthority? {
        events.append("window-authority")
        return nil
    }

    var presentation: VirtualCursorPresentation {
        events.append("presentation")
        return VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
    }

    func displayExclusionWindowID(availableWindows _: [VirtualCursorWindowRecord]) -> UInt32? {
        events.append("display-exclusion")
        return nil
    }

    func show(at _: CGPoint) throws { events.append("show") }
    func move(to _: CGPoint) throws { events.append("move") }
    func click(at _: CGPoint) throws { events.append("click") }
    func hide() { events.append("hide") }
    func close() { events.append("close") }
}

private final class StaticWindows: WindowObserving {
    var snapshotScopes: [String] = []
    var selectCount = 0
    var getAppStateCalls = 0
    var getAppStateScopes: [String] = []
    var getAppStateError: WindowObservationError?
    var snapshotError: WindowObservationError?

    func apps() throws -> JSONValue { .object(["apps": .array([])]) }

    func select(appRef: String, windowRef: String) throws -> WindowTarget {
        selectCount += 1
        return WindowTarget(
            appRef: appRef,
            windowRef: windowRef,
            pid: 99,
            windowID: 42,
            bounds: .zero,
            title: "",
            axIdentity: 7,
            interactionMode: .background
        )
    }

    func snapshot(appRef: String, windowRef: String, scope: String, artifactName: String?, textDetail _: SnapshotTextDetailRequest) throws -> JSONValue {
        snapshotScopes.append(scope)
        if let snapshotError { throw snapshotError }
        return .object([
            "snapshot_id": .string("snapshot_1"),
            "payload": .object([
                "image_artifact": .string(artifactName ?? "capture.png"),
                "logical_size": .object(["width": .number(1), "height": .number(1)]),
                "pixel_size": .object(["width": .number(1), "height": .number(1)]),
                "backing_scale": .number(1),
                "capture_bounds": .object([
                    "x": .number(0), "y": .number(0),
                    "width": .number(1), "height": .number(1),
                ]),
                "ax_tree": .object([:]),
            ]),
        ])
    }

    func getAppState(
        appRef: String,
        windowRef: String,
        catalogGeneration: Int,
        scope: String,
        artifactName: String,
        textDetail _: SnapshotTextDetailRequest
    ) throws -> AppStateObservation {
        getAppStateCalls += 1
        getAppStateScopes.append(scope)
        if let getAppStateError { throw getAppStateError }
        return AppStateObservation(
            target: WindowTarget(
                appRef: appRef,
                windowRef: windowRef,
                pid: 99,
                windowID: 42,
                bounds: .zero,
                title: "",
                axIdentity: 7,
                interactionMode: .background
            ),
            catalogGeneration: catalogGeneration,
            snapshot: .object([
                "snapshot_id": .string("snapshot_1"),
                "payload": .object([
                    "image_artifact": .string(artifactName),
                ]),
            ])
        )
    }
}

private final class CatalogActivationSpy: ApplicationActivationControlling {
    private(set) var activateCount = 0

    func activate(_: WindowTarget) throws { activateCount += 1 }
    func restore(pid _: pid_t) throws {}
}

private func catalogObservation(appRef: String, windowRef: String) -> WindowCatalogObservation {
    let target = catalogTarget(appRef: appRef, windowRef: windowRef)
    return WindowCatalogObservation(
        targets: [windowRef: target],
        apps: [
            .object([
                "app_ref": .string(appRef),
                "name": .string("Catalog App"),
                "bundle_id": .null,
                "app_version": .null,
                "windows": .array([
                    .object([
                        "window_ref": .string(windowRef),
                        "title": .string(target.title),
                        "bounds": CGRectJSON.encode(target.bounds),
                    ]),
                ]),
            ]),
        ]
    )
}

private func catalogTarget(
    appRef: String,
    windowRef: String,
    pid: pid_t = 7,
    windowID: CGWindowID = 9,
    bounds: CGRect = CGRect(x: 1, y: 2, width: 300, height: 200),
    axIdentity: CFHashCode? = 7
) -> WindowTarget {
    WindowTarget(
        appRef: appRef,
        windowRef: windowRef,
        pid: pid,
        windowID: windowID,
        bounds: bounds,
        title: "Catalog Window",
        axIdentity: axIdentity
    )
}

private func catalogWindowIdentities(_ value: JSONValue) -> [String] {
    guard case let .object(catalog) = value,
          case let .array(apps)? = catalog["apps"]
    else { return [] }
    return apps.flatMap { appValue -> [String] in
        guard case let .object(app) = appValue,
              case let .array(windows)? = app["windows"]
        else { return [] }
        return windows.compactMap { windowValue in
            guard case let .object(window) = windowValue else { return nil }
            return jsonString(window["window_identity_ref"])
        }
    }
}

private func firstCatalogWindowIdentity(_ value: JSONValue) -> String? {
    catalogWindowIdentities(value).first
}

private func catalogGeneration(_ value: JSONValue) -> Int? {
    guard case let .object(values) = value,
          case let .number(generation)? = values["catalog_generation"],
          generation.isFinite,
          generation.rounded(.towardZero) == generation
    else { return nil }
    return Int(generation)
}

private func jsonString(_ value: JSONValue?) -> String? {
    guard case let .string(string)? = value else { return nil }
    return string
}

private func jsonBool(_ value: JSONValue?) -> Bool? {
    guard case let .bool(bool)? = value else { return nil }
    return bool
}

private func jsonBounds(_ value: JSONValue?) -> [String: Double]? {
    guard case let .object(values)? = value else { return nil }
    var result: [String: Double] = [:]
    for key in ["x", "y", "width", "height"] {
        guard case let .number(number)? = values[key] else { return nil }
        result[key] = number
    }
    return result
}

private enum AppStateProductionTransactionFault {
    case pngCapture
    case axSerialization
    case responseShape
    case responseScopeMismatch
    case secondArtifactWrite
    case secondArtifactSync
    case secondArtifactRename
    case directorySync
    case finalIdentityDrift
    case cancellation
}

private enum AppStateProductionTransactionTestError: Error {
    case pngCapture
    case axSerialization
    case cancellation
}

private struct AppStatePreparedAXFixture {
    let detail: Data
}

private final class AppStateProductionTransactionEnvironment {
    var frontmostPID: pid_t? = 999
    var realPointer = CGPoint(x: 400, y: 300)
    private(set) var frontmostReads = 0
    private(set) var pointerReads = 0

    func observe() -> (pid_t?, CGPoint) {
        frontmostReads += 1
        pointerReads += 1
        return (frontmostPID, realPointer)
    }
}

private final class AppStateProductionTransactionCatalog: TargetCataloging {
    let selectedElement = AXUIElementCreateApplication(42)
    var current: TargetCatalogRecord

    init() {
        current = TargetCatalogRecord(
            appRef: "app",
            windowRef: "window",
            pid: 42,
            windowID: 24,
            bounds: CGRect(x: 10, y: 20, width: 300, height: 200),
            title: "Document",
            axWindows: []
        )
        current = record(bounds: current.bounds)
    }

    func record(appRef: String, windowRef: String) throws -> TargetCatalogRecord? {
        guard appRef == current.appRef, windowRef == current.windowRef else { return nil }
        return current
    }

    func currentRecord(for _: WindowTarget) throws -> TargetCatalogRecord? { current }

    func driftIdentity() {
        current = record(bounds: current.bounds, identity: 701)
    }

    private func record(bounds: CGRect, identity: CFHashCode = 700) -> TargetCatalogRecord {
        TargetCatalogRecord(
            appRef: "app",
            windowRef: "window",
            pid: 42,
            windowID: 24,
            bounds: bounds,
            title: "Document",
            axWindows: [TargetAXWindowRecord(
                windowID: 24,
                bounds: bounds,
                identity: identity,
                element: selectedElement
            )]
        )
    }
}

private func runAppStateProductionTransactionFixture(
    fault: AppStateProductionTransactionFault?,
    root: URL,
    descriptor: Int32
) throws {
    let names = smartBundleNames(40)
    let syscallFault: BundleTestArtifactSyscalls.Fault
    switch fault {
    case .secondArtifactWrite: syscallFault = .write(2)
    case .secondArtifactSync: syscallFault = .fileSync(2)
    case .secondArtifactRename: syscallFault = .rename(2)
    case .directorySync: syscallFault = .directorySync
    default: syscallFault = .none
    }
    let syscalls = BundleTestArtifactSyscalls(directoryFD: descriptor, fault: syscallFault)
    let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
    defer { publisher.close() }
    let activation = CatalogActivationSpy()
    let catalog = AppStateProductionTransactionCatalog()
    let controller = BackgroundTargetController(catalog: catalog, activation: activation)
    let candidate = try controller.select(appRef: "app", windowRef: "window")
    _ = try controller.snapshotTargetState(candidate)
    let environment = AppStateProductionTransactionEnvironment()
    let initialFrontmostPID = environment.frontmostPID
    let initialPointer = environment.realPointer
    let references = SnapshotReferenceRegistry()
    let original = WindowTarget(
        appRef: "app",
        windowRef: "window",
        pid: 1,
        windowID: 1,
        bounds: CGRect(x: 0, y: 0, width: 1, height: 1),
        title: "Catalog placeholder"
    )
    var targetRegistry = ["window": original]
    var events: [String] = ["select", "revalidate"]
    var commitCount = 0
    let request = GetAppStateRequest(
        appRef: "app",
        windowRef: "window",
        catalogGeneration: 7,
        scope: "target_window",
        artifactName: names.image,
        textDetail: .on(artifactName: names.detail)
    )

    do {
        let completed: SnapshotCaptureTransactionResult<AppStateObservation> = try completeSnapshotCaptureTransaction(
            capturePNG: {
                events.append("png")
                if fault == .pngCapture { throw AppStateProductionTransactionTestError.pngCapture }
                return Data("png".utf8)
            },
            serializeAX: {
                events.append("ax")
                if fault == .axSerialization { throw AppStateProductionTransactionTestError.axSerialization }
                return AppStatePreparedAXFixture(detail: Data("detail".utf8))
            },
            buildSnapshot: { _, _ in
                events.append("shape")
                return appStateProductionTransactionSnapshot(
                    imageName: fault == .responseShape ? "wrong.png" : names.image,
                    detailName: names.detail,
                    includeDisplayMetadata: fault == .responseScopeMismatch
                )
            },
            prepareCommit: { snapshot in
                events.append("prepare")
                let observation = AppStateObservation(
                    target: candidate,
                    catalogGeneration: 7,
                    snapshot: snapshot
                )
                guard validGetAppStateResponse(
                    result: observation.helperResultJSON,
                    snapshot: snapshot,
                    request: request
                ) else { throw WindowObservationError.captureFailed }
                return SnapshotFinalCommit(
                    result: observation,
                    commit: {
                        #expect(syscalls.directorySyncCalls == 1)
                        #expect(FileManager.default.fileExists(
                            atPath: root.appendingPathComponent(names.image).path
                        ))
                        #expect(FileManager.default.fileExists(
                            atPath: root.appendingPathComponent(names.detail).path
                        ))
                        events.append("commit")
                        commitCount += 1
                        targetRegistry["window"] = candidate
                    }
                )
            },
            validateFinalIdentity: {
                events.append("final_identity")
                let observed = environment.observe()
                #expect(observed.0 == initialFrontmostPID)
                #expect(observed.1 == initialPointer)
                if fault == .finalIdentityDrift { catalog.driftIdentity() }
                if fault == .cancellation {
                    throw AppStateProductionTransactionTestError.cancellation
                }
                return try controller.snapshotTargetState(candidate)
            },
            publish: { png, prepared in
                events.append("publish")
                _ = try publisher.publish(
                    image: png,
                    imageName: names.image,
                    detail: prepared.detail,
                    detailName: names.detail
                )
            },
            register: { _, _ in
                events.append("register")
                references.register(
                    snapshotID: "snapshot_tx",
                    references: ["element_0": SnapshotElement(element: nil, bounds: .zero)]
                )
            }
        )

        guard fault == nil else {
            Issue.record("fault \(String(describing: fault)) unexpectedly succeeded")
            return
        }
        #expect(completed.result.target.interactionMode == .background)
        #expect(commitCount == 1)
        #expect(targetRegistry["window"]?.interactionMode == .background)
        #expect(references.element(for: "element_0", snapshotID: "snapshot_tx") != nil)
        #expect(events == [
            "select", "revalidate", "png", "ax", "shape", "prepare",
            "final_identity", "publish", "register", "commit",
        ])
    } catch {
        guard fault != nil else {
            Issue.record("success transaction failed: \(error)")
            return
        }
        #expect(commitCount == 0)
        #expect(targetRegistry["window"]?.windowID == original.windowID)
        #expect(references.element(for: "element_0", snapshotID: "snapshot_tx") == nil)
        let entries = try bundleDirectoryEntries(root)
        #expect(!entries.contains(names.image))
        #expect(!entries.contains(names.detail))
        let expectedEvents: [String]
        switch fault {
        case .pngCapture:
            expectedEvents = ["select", "revalidate", "png"]
        case .axSerialization:
            expectedEvents = ["select", "revalidate", "png", "ax"]
        case .responseShape, .responseScopeMismatch:
            expectedEvents = ["select", "revalidate", "png", "ax", "shape", "prepare"]
        case .finalIdentityDrift, .cancellation:
            expectedEvents = [
                "select", "revalidate", "png", "ax", "shape", "prepare",
                "final_identity",
            ]
        case .secondArtifactWrite, .secondArtifactSync, .secondArtifactRename, .directorySync:
            expectedEvents = [
                "select", "revalidate", "png", "ax", "shape", "prepare",
                "final_identity", "publish",
            ]
        case nil:
            expectedEvents = []
        }
        #expect(events == expectedEvents)
    }
    #expect(activation.activateCount == 0)
    #expect(environment.frontmostPID == initialFrontmostPID)
    #expect(environment.realPointer == initialPointer)
    if fault == .secondArtifactWrite { #expect(syscalls.writeCalls == 2) }
    if fault == .secondArtifactSync { #expect(syscalls.fileSyncCalls == 2) }
    if fault == .secondArtifactRename { #expect(syscalls.finalRenameCalls == 2) }
    if fault == .directorySync { #expect(syscalls.directorySyncCalls >= 1) }
}

private func appStateProductionTransactionSnapshot(
    imageName: String,
    detailName: String,
    includeDisplayMetadata: Bool = false
) -> JSONValue {
    var payload: [String: JSONValue] = [
        "image_artifact": .string(imageName),
        "logical_size": .object(["width": .number(1), "height": .number(1)]),
        "pixel_size": .object(["width": .number(1), "height": .number(1)]),
        "backing_scale": .number(1),
        "capture_bounds": .object([
            "x": .number(0), "y": .number(0),
            "width": .number(1), "height": .number(1),
        ]),
        "ax_tree": .object(["role": .string("AXWindow")]),
        "text_detail_artifact": .string(detailName),
        "text_detail_metadata": .object([
            "schema_version": .number(1),
            "snapshot_id": .string("snapshot_tx"),
            "coverage": .string("reported_ax_subtree"),
            "node_count": .number(1),
            "max_depth_observed": .number(0),
            "byte_count": .number(6),
            "sha256": .string(String(repeating: "a", count: 64)),
            "truncated": .bool(false),
            "truncation_reasons": .array([]),
        ]),
    ]
    if includeDisplayMetadata {
        payload["display_id"] = .number(7)
        payload["target_window_bounds"] = .object([
            "x": .number(0), "y": .number(0),
            "width": .number(1), "height": .number(1),
        ])
    }
    return .object([
        "snapshot_id": .string("snapshot_tx"),
        "payload": .object(payload),
    ])
}

private func smartBundleNames(_ index: Int) -> (image: String, detail: String) {
    let suffix = String(index, radix: 16)
    let token = String(repeating: "0", count: 32 - suffix.count) + suffix
    return ("snapshot-\(token).png", "snapshot-\(token).ax.json")
}

private func withBundleTestDirectory(_ body: (URL, Int32) throws -> Void) throws {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("astra-bundle-\(UUID().uuidString)")
    try FileManager.default.createDirectory(
        at: root,
        withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o700]
    )
    let descriptor = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    #expect(descriptor >= 0)
    defer {
        if descriptor >= 0 { _ = Darwin.close(descriptor) }
        try? FileManager.default.removeItem(at: root)
    }
    guard descriptor >= 0 else { return }
    try body(root, descriptor)
}

private func bundleDirectoryEntries(_ root: URL) throws -> [String] {
    try FileManager.default.contentsOfDirectory(atPath: root.path).sorted()
}

/// Close-fault tests free descriptors and then deliberately probe or reuse their numbers. Parallel
/// Swift Testing shares one process, and every other test allocates the lowest free numbers, so a
/// low descriptor can meanwhile belong to another test: a probe then misreports it, and `dup2`
/// or `close` can replace another test's pipe and leave that test waiting. These descriptors
/// therefore live in a private high range.
private enum BundleTestDescriptorRange {
    private static let lock = NSLock()
    private static var nextSlot: Int32 = 0
    private static let base: Int32 = 8192
    private static let slotWidth: Int32 = 16
    private static let slots: Int32 = 60

    static func relocate(_ descriptor: Int32) -> Int32 {
        guard descriptor >= 0, softLimitAllows(base + slots * slotWidth) else { return descriptor }
        lock.lock()
        let slot = nextSlot % slots
        nextSlot += 1
        lock.unlock()
        let moved = Darwin.fcntl(descriptor, F_DUPFD_CLOEXEC, base + slot * slotWidth)
        guard moved >= 0 else { return descriptor }
        _ = Darwin.close(descriptor)
        return moved
    }

    private static func softLimitAllows(_ wanted: Int32) -> Bool {
        lock.lock(); defer { lock.unlock() }
        var limit = rlimit()
        guard getrlimit(RLIMIT_NOFILE, &limit) == 0 else { return false }
        if limit.rlim_cur >= rlim_t(wanted) { return true }
        guard limit.rlim_max >= rlim_t(wanted) else { return false }
        limit.rlim_cur = rlim_t(wanted)
        return setrlimit(RLIMIT_NOFILE, &limit) == 0
    }
}

final class BundleTestArtifactSyscalls: ArtifactSyscalls {
    enum Fault: Equatable {
        case none
        case open(Int)
        case descriptorStatus(Int)
        case fileMode(Int)
        case specialFileMode(Int)
        case unsafeDirectoryAfterFileSync
        case directoryRead
        case replaceRollbackTarget
        case replaceQuarantineAfterStatus
        case initialAndRecoveryStatusFailingCloseStillOpen
        case write(Int)
        case fileSync(Int)
        case close(Int)
        case closeStillOpen(Int)
        case closeAndReportEBADF(Int)
        case rename(Int)
        case directorySync

        /// These faults free descriptors that the tests then probe or deliberately reuse.
        var reusesDescriptorNumbers: Bool {
            switch self {
            case .close, .closeStillOpen, .closeAndReportEBADF, .initialAndRecoveryStatusFailingCloseStillOpen:
                true
            default:
                false
            }
        }
    }

    let directoryFD: Int32
    let fault: Fault
    private(set) var events: [String] = []
    private(set) var openCalls = 0
    private(set) var descriptorStatusCalls = 0
    private(set) var fileModeCalls = 0
    private(set) var writeCalls = 0
    private(set) var fileSyncCalls = 0
    private(set) var closeCalls = 0
    private(set) var renameCalls = 0
    private(set) var finalRenameCalls = 0
    private(set) var directorySyncCalls = 0
    private(set) var failedCloseFD: Int32?
    private(set) var postFailureStatusCalls = 0
    private(set) var failedCloseFDRecoveryCloseCalls = 0
    private(set) var injectedCloseErrnos: [Int32] = []
    private(set) var unlinkCalls = 0
    private var replacementAttackPerformed = false

    var firstRenameEventIndex: Int? { events.firstIndex(of: "rename") }

    init(directoryFD: Int32, fault: Fault = .none) {
        self.directoryFD = directoryFD
        self.fault = fault
    }

    func openFile(at directoryFD: Int32, name: String, flags: Int32, mode: mode_t) -> Int32 {
        openCalls += 1
        events.append("open")
        if fault == .open(openCalls) {
            errno = EIO
            return -1
        }
        let descriptor = name.withCString { Darwin.openat(directoryFD, $0, flags, mode) }
        return fault.reusesDescriptorNumbers ? BundleTestDescriptorRange.relocate(descriptor) : descriptor
    }

    func directoryEntryNames(
        _ directoryFD: Int32,
        maximumEntries: Int,
        maximumNameBytes: Int
    ) throws -> Set<String> {
        if fault == .directoryRead { throw ArtifactBundleError.unsafeDirectory }
        return try defaultArtifactDirectoryEntryNames(
            directoryFD,
            maximumEntries: maximumEntries,
            maximumNameBytes: maximumNameBytes
        )
    }

    func writeFile(_ fileDescriptor: Int32, buffer: UnsafeRawPointer, count: Int) -> Int {
        writeCalls += 1
        events.append("write")
        if fault == .write(writeCalls)
            || ((fault == .replaceRollbackTarget || fault == .replaceQuarantineAfterStatus) && writeCalls == 1)
        {
            errno = EIO
            return -1
        }
        return Darwin.write(fileDescriptor, buffer, count)
    }

    func status(_ fileDescriptor: Int32, into value: UnsafeMutablePointer<stat>) -> Int32 {
        descriptorStatusCalls += 1
        events.append("descriptor-status")
        if failedCloseFD == fileDescriptor { postFailureStatusCalls += 1 }
        if failedCloseFD == fileDescriptor,
           case .closeAndReportEBADF = fault
        {
            errno = EBADF
            return -1
        }
        if case .initialAndRecoveryStatusFailingCloseStillOpen = fault {
            errno = EIO
            return -1
        }
        if fault == .descriptorStatus(descriptorStatusCalls) {
            errno = EIO
            return -1
        }
        return Darwin.fstat(fileDescriptor, value)
    }

    func setMode(_ fileDescriptor: Int32, mode: mode_t) -> Int32 {
        fileModeCalls += 1
        events.append("file-mode")
        if fault == .fileMode(fileModeCalls) {
            errno = EIO
            return -1
        }
        if fault == .specialFileMode(fileModeCalls) {
            return Darwin.fchmod(fileDescriptor, mode_t(0o4600))
        }
        return Darwin.fchmod(fileDescriptor, mode)
    }

    func sync(_ fileDescriptor: Int32) -> Int32 {
        if fileDescriptor == directoryFD {
            directorySyncCalls += 1
            events.append("directory-sync")
            if fault == .directorySync, directorySyncCalls == 1 {
                errno = EIO
                return -1
            }
        } else {
            fileSyncCalls += 1
            events.append("file-sync")
            if fault == .fileSync(fileSyncCalls) {
                errno = EIO
                return -1
            }
            if fault == .unsafeDirectoryAfterFileSync, fileSyncCalls == 2 {
                _ = Darwin.fchmod(directoryFD, mode_t(0o755))
            }
        }
        return Darwin.fsync(fileDescriptor)
    }

    func closeFile(_ fileDescriptor: Int32) -> Int32 {
        closeCalls += 1
        events.append("close")
        if failedCloseFD == fileDescriptor { failedCloseFDRecoveryCloseCalls += 1 }
        if fault == .closeStillOpen(closeCalls) {
            failedCloseFD = fileDescriptor
            errno = EINTR
            injectedCloseErrnos.append(errno)
            return -1
        }
        if case .initialAndRecoveryStatusFailingCloseStillOpen = fault {
            failedCloseFD = fileDescriptor
            errno = EINTR
            injectedCloseErrnos.append(errno)
            return -1
        }
        let result = Darwin.close(fileDescriptor)
        if fault == .close(closeCalls)
            || fault == .closeAndReportEBADF(closeCalls)
        {
            failedCloseFD = fileDescriptor
            errno = EINTR
            injectedCloseErrnos.append(errno)
            return -1
        }
        return result
    }

    func renameExclusive(at directoryFD: Int32, from temporary: String, to final: String) -> Int32 {
        renameCalls += 1
        if !final.hasSuffix(".quarantine") { finalRenameCalls += 1 }
        events.append("rename")
        if fault == .rename(renameCalls) {
            errno = EIO
            return -1
        }
        if fault == .replaceRollbackTarget,
           final.hasSuffix(".quarantine"),
           !replacementAttackPerformed
        {
            replacementAttackPerformed = true
            _ = ".attacker-stolen".withCString { stolen in
                temporary.withCString { source in
                    Darwin.renameatx_np(directoryFD, source, directoryFD, stolen, UInt32(RENAME_EXCL))
                }
            }
            let replacement = temporary.withCString {
                Darwin.openat(directoryFD, $0, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode_t(0o600))
            }
            if replacement >= 0 {
                _ = Data("replacement".utf8).withUnsafeBytes {
                    Darwin.write(replacement, $0.baseAddress, $0.count)
                }
                _ = Darwin.close(replacement)
            }
        }
        return temporary.withCString { source in
            final.withCString { destination in
                Darwin.renameatx_np(
                    directoryFD,
                    source,
                    directoryFD,
                    destination,
                    UInt32(RENAME_EXCL)
                )
            }
        }
    }

    func unlink(at directoryFD: Int32, name: String) -> Int32 {
        unlinkCalls += 1
        events.append("unlink")
        return name.withCString { Darwin.unlinkat(directoryFD, $0, 0) }
    }

    func status(at directoryFD: Int32, name: String, into value: UnsafeMutablePointer<stat>) -> Int32 {
        let result = name.withCString { Darwin.fstatat(directoryFD, $0, value, AT_SYMLINK_NOFOLLOW) }
        if result == 0,
           fault == .replaceQuarantineAfterStatus,
           name.hasSuffix(".quarantine"),
           !replacementAttackPerformed
        {
            replacementAttackPerformed = true
            _ = ".attacker-stolen".withCString { stolen in
                name.withCString { source in
                    Darwin.renameatx_np(directoryFD, source, directoryFD, stolen, UInt32(RENAME_EXCL))
                }
            }
            let replacement = name.withCString {
                Darwin.openat(directoryFD, $0, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode_t(0o600))
            }
            if replacement >= 0 {
                _ = Data("replacement".utf8).withUnsafeBytes {
                    Darwin.write(replacement, $0.baseAddress, $0.count)
                }
                _ = Darwin.close(replacement)
            }
        }
        if result == 0,
           fault == .replaceRollbackTarget,
           name.hasSuffix(".tmp"),
           !replacementAttackPerformed
        {
            replacementAttackPerformed = true
            _ = ".attacker-stolen".withCString { stolen in
                name.withCString { source in
                    Darwin.renameatx_np(directoryFD, source, directoryFD, stolen, UInt32(RENAME_EXCL))
                }
            }
            let replacement = name.withCString {
                Darwin.openat(directoryFD, $0, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, mode_t(0o600))
            }
            if replacement >= 0 {
                _ = Data("replacement".utf8).withUnsafeBytes {
                    Darwin.write(replacement, $0.baseAddress, $0.count)
                }
                _ = Darwin.close(replacement)
            }
        }
        return result
    }
}

@Test func windowTitlesMatchAcceptsChromiumAppSuffixInBothDirections() {
    // accessibility title carries the "<page> - <app>" suffix (Edge form)
    #expect(windowTitlesMatch(
        screenCaptureTitle: "Astra Edge Probe",
        accessibilityTitle: "Astra Edge Probe - Microsoft Edge"
    ))
    // screen capture title carries the suffix (reverse direction)
    #expect(windowTitlesMatch(
        screenCaptureTitle: "Astra Edge Probe - Microsoft Edge",
        accessibilityTitle: "Astra Edge Probe"
    ))
    // bare title still matches exactly
    #expect(windowTitlesMatch(
        screenCaptureTitle: "astra-vscode-probe.txt — Project",
        accessibilityTitle: "astra-vscode-probe.txt — Project"
    ))
    // empty screen capture title remains a match (no evidence to contradict)
    #expect(windowTitlesMatch(screenCaptureTitle: "", accessibilityTitle: "anything"))
}

@Test func windowTitlesMatchRejectsUnrelatedOrTooShortSuffices() {
    // different page plus suffix must not match
    #expect(!windowTitlesMatch(
        screenCaptureTitle: "Astra Edge Probe",
        accessibilityTitle: "Another Page - Microsoft Edge"
    ))
    // suffix without the " - " separator must not match
    #expect(!windowTitlesMatch(
        screenCaptureTitle: "Astra Edge Probe",
        accessibilityTitle: "Astra Edge Probe Microsoft Edge"
    ))
    // prefix without any extension must not count as suffix match
    #expect(!windowTitlesMatch(
        screenCaptureTitle: "Astra Edge Probe",
        accessibilityTitle: "Astra"
    ))
    // path titles keep their last-path-component rule
    #expect(!windowTitlesMatch(
        screenCaptureTitle: "/tmp/other.txt",
        accessibilityTitle: "/tmp/astra-probe.txt — Project"
    ))
}

@Test func windowCaptureMetadataFollowsExactImageResolution() throws {
    let requested = WindowGeometry(bounds: CGRect(x: 90, y: 120, width: 1084, height: 684), backingScale: 2)
    let retina = try resolvedWindowImageGeometry(requested: requested, imageWidth: 2168, imageHeight: 1368)
    #expect(retina.backingScale == 2)
    let logical = try resolvedWindowImageGeometry(requested: requested, imageWidth: 1084, imageHeight: 684)
    #expect(logical.backingScale == 1)
    #expect(logical.pixelSize == CGSize(width: 1084, height: 684))
    #expect(logical.bounds == requested.bounds)
    #expect(logical.screenPoint(for: CGPoint(x: 150, y: 27)) == CGPoint(x: 240, y: 147))
    for size in [(1084, 1368), (2168, 684), (1083, 684), (1104, 704), (0, 0)] {
        #expect(throws: WindowObservationError.self) {
            try resolvedWindowImageGeometry(requested: requested, imageWidth: size.0, imageHeight: size.1)
        }
    }
}

@Test func transparentWindowCaptureIsUnavailableButOpaqueBlackIsValid() throws {
    let context = try #require(CGContext(
        data: nil, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 32,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ))
    context.clear(CGRect(x: 0, y: 0, width: 8, height: 8))
    let empty = try #require(context.makeImage())
    #expect(throws: WindowObservationError.self) { try validateWindowImageContent(empty) }
    context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    context.fill(CGRect(x: 7, y: 7, width: 1, height: 1))
    try validateWindowImageContent(#require(context.makeImage()))
    context.fill(CGRect(x: 0, y: 0, width: 8, height: 8))
    try validateWindowImageContent(#require(context.makeImage()))
    let opaqueContext = try #require(CGContext(
        data: nil, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 32,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ))
    opaqueContext.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    opaqueContext.fill(CGRect(x: 0, y: 0, width: 8, height: 8))
    try validateWindowImageContent(#require(opaqueContext.makeImage()))
}

@Test func transparentWindowPixelsKeepSpecificErrorAcrossObservationEntryPoints() throws {
    let context = try #require(CGContext(
        data: nil, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 32,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ))
    context.clear(CGRect(x: 0, y: 0, width: 8, height: 8))
    let image = try #require(context.makeImage())
    do {
        try validateWindowImageContent(image)
        Issue.record("empty pixels must not authorize actions")
    } catch let error as WindowObservationError {
        let observer = StaticWindows()
        observer.getAppStateError = error
        let response = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
            #"{"protocol_version":4,"request_id":"empty-image","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
        )
        #expect(!response.ok)
        #expect(response.error?.code == "window_content_unavailable")
        #expect(response.snapshot == nil)
        #expect(response.result == nil)
        observer.snapshotError = error
        let snapshotResponse = Dispatcher(permissions: StaticPermissions(), windows: observer).handle(
            #"{"protocol_version":4,"request_id":"empty-snapshot","operation":"snapshot","payload":{"app_ref":"app_1","window_ref":"win_1","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
        )
        #expect(!snapshotResponse.ok)
        #expect(snapshotResponse.error?.code == "window_content_unavailable")
        #expect(snapshotResponse.snapshot == nil)
        #expect(snapshotResponse.result == nil)
    }
}

@Test func overlayErrorSurvivesObservationWireContracts() {
    let observer = StaticWindows()
    observer.getAppStateError = WindowObservationError.overlayBlocked
    observer.snapshotError = WindowObservationError.overlayBlocked
    let dispatcher = Dispatcher(permissions: StaticPermissions(), windows: observer)
    let state = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"overlay-state","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    let snapshot = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"overlay-snapshot","operation":"snapshot","payload":{"app_ref":"app_1","window_ref":"win_1","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    for response in [state, snapshot] {
        #expect(!response.ok)
        #expect(response.error?.code == "overlay_blocked")
        #expect(response.snapshot == nil)
    }
}

@Test func unmatchedWindowErrorSurvivesObservationWireContracts() {
    let observer = StaticWindows()
    observer.getAppStateError = WindowObservationError.axWindowUnmatched
    observer.snapshotError = WindowObservationError.axWindowUnmatched
    let dispatcher = Dispatcher(permissions: StaticPermissions(), windows: observer)
    let state = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"unmatched-state","operation":"get_app_state","payload":{"app_ref":"app_1","window_ref":"win_1","catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    let snapshot = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"unmatched-snapshot","operation":"snapshot","payload":{"app_ref":"app_1","window_ref":"win_1","scope":"target_window","artifact_name":"snapshot-0123456789abcdef0123456789abcdef.png"}}"#
    )
    for response in [state, snapshot] {
        #expect(!response.ok)
        #expect(response.error?.code == "ax_window_unmatched")
        #expect(response.snapshot == nil)
        #expect(response.result == nil)
    }
}
