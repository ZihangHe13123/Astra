import AstraMacComputerHelperCore
import Darwin
import Foundation

switch AppshotHelperMode(arguments: Array(CommandLine.arguments.dropFirst())) {
case .computerUse:
  runHelperMain()
case .daemon:
  exit(MainActor.assumeIsolated { runAppshotDaemon() })
case .clientIdentity:
  do {
    FileHandle.standardOutput.write(try appshotClientIdentityJSON() + Data([10]))
  } catch {
    FileHandle.standardError.write(Data("broker_unavailable\n".utf8))
    exit(69)
  }
case .brokerIdentity:
  do {
    FileHandle.standardOutput.write(try appshotBrokerIdentityJSON() + Data([10]))
  } catch {
    FileHandle.standardError.write(Data("broker_unavailable\n".utf8))
    exit(69)
  }
case .calendar:
  exit(runCalendarCommand(child: false))
case .calendarChild:
  exit(runCalendarCommand(child: true))
case .unsupported:
  FileHandle.standardError.write(Data("unsupported helper mode\n".utf8))
  exit(64)
}
