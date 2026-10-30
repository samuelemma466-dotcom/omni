import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Omni overlay: a floating chat surface for the permissioned local agent.
//
// The heavy lifting lives in `omni serve` (HTTP + SSE). This file is only the
// UI: it starts the daemon if needed, streams one chat turn at a time, and
// renders approval requests as inline cards. Every tool call still goes through
// the broker on the server; the overlay can approve or deny, never bypass.
Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  // ------------------------------------------------------------------- paths
  readonly property string home: Quickshell.env("HOME")
  readonly property string xdgStateHome: root.envOr("XDG_STATE_HOME", root.home + "/.local/state")
  readonly property string stateDir: root.xdgStateHome + "/omni"
  readonly property string tokenPath: root.stateDir + "/token"
  readonly property string pluginId: String(root.manifest && root.manifest.id ? root.manifest.id : "jovibe.omni")
  readonly property string pluginDir: root.home + "/.config/omarchy/plugins/" + root.pluginId
  readonly property string runScript: root.pluginDir + "/run-daemon.sh"
  readonly property string omniBin: root.envOr("OMNI_BIN", root.home + "/.local/bin/omni")
  readonly property int port: {
    var raw = Quickshell.env("OMNI_PORT")
    var parsed = raw ? parseInt(String(raw), 10) : NaN
    return isFinite(parsed) && parsed > 0 ? parsed : 8737
  }
  readonly property string baseUrl: "http://127.0.0.1:" + root.port

  // ------------------------------------------------------------------- state
  property bool opened: false
  property string token: ""
  property bool serverOk: false
  property bool starting: false
  property bool busy: false
  property bool stopping: false
  property string statusText: ""
  property double turnStartedAt: 0
  property int elapsedSeconds: 0
  property var history: []
  property int restarts: 0
  property double lastStartAt: 0
  property bool shuttingDown: false
  property string closeReason: ""

  // ------------------------------------------------------------------- theme
  property color background: Commons.Color.menu.background
  property color foreground: Commons.Color.menu.text
  property color muted: Commons.Color.muted
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property color scrim: Commons.Color.menu.scrim
  property color surface: Commons.Color.menu.selectedBackground
  property color borderColor: Commons.Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", borderColor, Math.max(1, Style.space(2)))

  readonly property int cornerRadius: Style.cornerRadius
  readonly property string fontFamily: Style.font.menuFamily
  readonly property string monoFamily: "monospace"
  readonly property int pad: Style.spacing.panelPadding
  readonly property int gap: Style.spacing.md
  readonly property int headerHeight: Math.max(Style.space(38), Style.font.title + Style.spacing.controlPaddingY * 2)
  readonly property int inputMinHeight: Math.max(Style.space(46), Style.font.body + Style.spacing.md * 2)
  readonly property int inputMaxHeight: Style.space(150)
  readonly property int cardWidth: Math.min(Style.space(760), panel.width - Style.gapsOut * 2)
  readonly property int cardHeight: Math.min(Style.space(660), panel.height - Style.gapsOut * 2)

  // --------------------------------------------------------------- lifecycle
  function envOr(name, fallback) {
    var value = Quickshell.env(name)
    return (value && String(value).length > 0) ? String(value) : fallback
  }

  function open(payloadJson) {
    console.info("omni: open()")
    root.opened = true
    root.checkHealth()
    Qt.callLater(function() {
      input.forceActiveFocus()
      transcriptList.positionViewAtEnd()
    })
  }

  function close() {
    console.info("omni: close()")
    root.closeReason = "close"
    root.opened = false
  }

  function dismiss() {
    console.info("omni: dismiss()")
    root.closeReason = "dismiss"
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function debugState() {
    return JSON.stringify({
      opened: root.opened,
      serverOk: root.serverOk,
      starting: root.starting,
      busy: root.busy,
      elapsedSeconds: root.elapsedSeconds,
      tokenLength: root.token.length,
      transcript: transcript.count,
      approvals: approvals.count,
      approvalStatuses: (function() { var out = []; for (var i = 0; i < approvals.count; i++) out.push(approvals.get(i).requestId + ":" + approvals.get(i).status); return out; })(),
      closeReason: root.closeReason,
      cardWidth: root.cardWidth,
      cardHeight: root.cardHeight,
      panelWidth: panel.width,
      panelHeight: panel.height
    })
  }

  function statusLabel() {
    if (root.serverOk) return "online"
    if (root.starting) return "starting"
    return "offline"
  }

  function statusTone() {
    if (root.serverOk) return root.accent
    if (root.starting) return root.muted
    return root.urgent
  }

  // ------------------------------------------------------------------ daemon
  function startDaemon() {
    if (root.shuttingDown) return
    if (daemonProc.running) return
    var now = Date.now()
    if (now - root.lastStartAt < 2500) return
    root.lastStartAt = now
    root.starting = true
    daemonProc.running = true
  }

  function checkHealth() {
    if (healthProc.running) return
    console.info("omni: health check")
    healthProc.running = true
  }

  function scheduleRestart() {
    if (root.shuttingDown) return
    if (root.restarts >= 20) return
    restartTimer.restart()
  }

  function startIfNeeded() {
    if (root.serverOk) return
    if (daemonProc.running) return
    root.scheduleRestart()
  }

  // ---------------------------------------------------------------- transcript
  function roleLabel(role) {
    switch (role) {
      case "user": return "YOU"
      case "assistant": return "OMNI"
      case "tool": return "TOOL"
      case "tool_result": return "RESULT"
      case "error": return "ERROR"
      default: return String(role || "").toUpperCase()
    }
  }

  function addMessage(role, text, meta) {
    transcript.append({
      role: String(role || "assistant"),
      text: String(text || ""),
      meta: String(meta || "")
    })
    root.scrollToEnd()
  }

  function scrollToEnd() {
    Qt.callLater(function() {
      if (root.opened) transcriptList.positionViewAtEnd()
    })
  }

  function clearTranscript() {
    transcript.clear()
    approvals.clear()
    root.history = []
    root.statusText = ""
  }

  // -------------------------------------------------------------------- chat
  function submit() {
    var text = input.text.trim()
    if (!text || root.busy) return
    if (!root.token) {
      root.addMessage("error", "No API token at " + root.tokenPath + ". Run `omni init` once, then try again.", "")
      return
    }
    if (!root.serverOk) {
      root.addMessage("error", "The omni daemon is not running yet. Starting it now — try again in a moment.", "")
      root.startDaemon()
      return
    }

    // A `done` event lands a few milliseconds before curl exits. Reap any
    // lingering process so a fast follow-up turn never reuses a live one.
    if (chatProc.running) chatProc.signal(15)

    root.addMessage("user", text, "")
    root.history = root.history.concat([{ role: "user", content: text }]).slice(-24)
    input.text = ""
    root.busy = true
    root.stopping = false
    root.statusText = "thinking"
    root.turnStartedAt = Date.now()
    root.elapsedSeconds = 0

    var body = JSON.stringify({ message: text, history: root.history.slice(0, -1) })
    chatProc.command = [
      "curl", "-sS", "-N", "--max-time", "900",
      "-X", "POST", root.baseUrl + "/api/chat",
      "-H", "Content-Type: application/json",
      "-H", "X-Omni-Token: " + root.token,
      "--data-binary", body
    ]
    chatProc.running = true
  }

  function stop() {
    if (!root.busy) return
    root.stopping = true
    for (var i = 0; i < approvals.count; i++) {
      if (approvals.get(i).status === "pending") root.decide(i, false, "once")
    }
    if (chatProc.running) chatProc.signal(15)
    root.busy = false
    root.statusText = ""
    root.addMessage("error", "Stopped.", "")
  }

  function handleSseLine(line) {
    if (!line || line.indexOf("data: ") !== 0) return
    var payload = line.slice(6)
    if (!payload) return
    var event
    try {
      event = JSON.parse(payload)
    } catch (error) {
      return
    }
    root.handleEvent(event)
  }

  function handleEvent(event) {
    if (!event || !event.type) return
    console.info("omni: event " + event.type)

    if (event.type === "status") {
      root.statusText = String(event.text || "working")
    } else if (event.type === "tool_call") {
      root.addMessage("tool", String(event.tool || "tool"), "running")
      root.statusText = "running " + String(event.tool || "tool")
    } else if (event.type === "tool_result") {
      var detail = event.ok ? "ok" : String(event.error || "failed")
      root.addMessage("tool_result", String(event.tool || "tool"), detail)
      root.statusText = "thinking"
    } else if (event.type === "approval_required") {
      root.pushApproval(event.request)
    } else if (event.type === "assistant") {
      var content = String(event.content || "")
      root.addMessage("assistant", content, "")
      root.history = root.history.concat([{ role: "assistant", content: content }]).slice(-24)
      root.statusText = ""
    } else if (event.type === "error") {
      root.addMessage("error", String(event.message || "unknown error"), "")
      root.statusText = ""
    } else if (event.type === "done") {
      root.busy = false
      root.statusText = ""
      root.expireApprovals()
    }
  }

  // ---------------------------------------------------------------- approvals
  function pushApproval(request) {
    console.info("omni: pushApproval " + JSON.stringify(request))
    if (!request || !request.id) return
    for (var i = 0; i < approvals.count; i++) {
      if (approvals.get(i).requestId === request.id) return
    }
    approvals.append({
      requestId: String(request.id),
      tool: String(request.tool || ""),
      danger: String(request.danger || "medium"),
      reason: String(request.reason || ""),
      capabilities: (request.capabilities || []).join(", "),
      preview: String(request.preview || ""),
      argsJson: JSON.stringify(request.args || {}),
      status: "pending"
    })
  }

  function decide(index, approved, scope) {
    if (index < 0 || index >= approvals.count) return
    var row = approvals.get(index)
    if (!row || row.status !== "pending") return
    approvals.setProperty(index, "status", approved ? "approved" : "denied")
    var payload = JSON.stringify({ approved: approved, scope: scope || "once" })
    Quickshell.execDetached([
      "curl", "-sS", "--max-time", "10",
      "-X", "POST", root.baseUrl + "/api/approvals/" + row.requestId,
      "-H", "Content-Type: application/json",
      "-H", "X-Omni-Token: " + root.token,
      "--data-binary", payload
    ])
  }

  function removeApproval(index) {
    if (index >= 0 && index < approvals.count) approvals.remove(index)
  }

  // A dropped stream or a finished turn leaves no request the broker will
  // still accept. Mark them expired instead of showing dead buttons.
  function expireApprovals() {
    for (var i = 0; i < approvals.count; i++) {
      if (approvals.get(i).status === "pending") approvals.setProperty(i, "status", "expired")
    }
  }

  // ------------------------------------------------------------------- models
  ListModel { id: transcript }
  ListModel { id: approvals }

  // ---------------------------------------------------------------- processes
  FileView {
    path: root.tokenPath
    watchChanges: true
    onLoaded: root.token = String(text()).trim()
    onFileChanged: root.token = String(text()).trim()
  }

  Process {
    id: healthProc
    command: ["curl", "-fsS", "--max-time", "2", root.baseUrl + "/health"]
    stdout: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      var ok = exitCode === 0
      console.info("omni: health exit " + exitCode + " ok=" + ok)
      root.serverOk = ok
      if (ok) {
        root.starting = false
        root.restarts = 0
      } else {
        root.startIfNeeded()
      }
    }
  }

  Process {
    id: chatProc
    command: []
    stdout: SplitParser {
      onRead: function(line) { root.handleSseLine(line) }
    }
    stderr: SplitParser {
      onRead: function(line) {
        if (line && String(line).trim().length > 0) root.statusText = String(line).trim()
      }
    }
    onExited: function(exitCode) {
      if (root.stopping) {
        root.stopping = false
        return
      }
      if (root.busy) {
        root.busy = false
        root.statusText = ""
        if (exitCode !== 0)
          root.addMessage("error", "Lost the connection to omni (curl exit " + exitCode + ").", "")
        root.expireApprovals()
        root.checkHealth()
      }
    }
  }

  Process {
    id: daemonProc
    command: ["bash", root.runScript]
    onStarted: root.starting = true
    onExited: function(exitCode) {
      root.starting = false
      if (root.shuttingDown) return
      if (exitCode === 78) {
        root.restarts = 20
        root.addMessage("error", "Cannot find the omni executable at " + root.omniBin + ". Run the plugin installer.", "")
        return
      }
      if (exitCode === 0) {
        root.checkHealth()
        return
      }
      root.restarts += 1
      root.scheduleRestart()
    }
  }

  Timer {
    id: restartTimer
    interval: 2000
    repeat: false
    onTriggered: root.startDaemon()
  }

  Timer {
    id: bootTimer
    interval: 1500
    repeat: false
    onTriggered: {
      root.checkHealth()
      root.startDaemon()
    }
  }

  Timer {
    id: elapsedTimer
    interval: 1000
    repeat: true
    running: root.busy
    onTriggered: root.elapsedSeconds = Math.max(0, Math.round((Date.now() - root.turnStartedAt) / 1000))
  }

  Timer {
    id: healthTimer
    interval: 10000
    repeat: true
    // Poll while the overlay is open, and keep polling until the daemon is
    // healthy. A healthy, closed overlay costs nothing.
    running: root.opened || !root.serverOk
    onTriggered: root.checkHealth()
  }

  Component.onCompleted: bootTimer.start()
  Component.onDestruction: {
    root.shuttingDown = true
    if (daemonProc.running) daemonProc.running = false
    if (chatProc.running) chatProc.signal(15)
  }

  // ------------------------------------------------------------- small widgets
  component PillButton: Rectangle {
    id: button
    property string label: ""
    property color tint: root.accent
    property bool filled: false
    signal clicked()

    implicitWidth: labelText.implicitWidth + Style.spacing.lg * 2
    implicitHeight: labelText.implicitHeight + Style.spacing.sm * 2
    width: implicitWidth
    height: implicitHeight
    radius: root.cornerRadius
    color: button.filled ? button.tint : "transparent"
    border.width: 1
    border.color: button.tint
    opacity: button.enabled ? 1 : 0.4

    Text {
      id: labelText
      anchors.centerIn: parent
      text: button.label
      color: button.filled ? root.background : button.tint
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: button.clicked()
    }
  }

  component MessageRow: Item {
    id: row
    required property string role
    required property string text
    required property string meta

    readonly property bool isUser: role === "user"
    readonly property bool isTool: role === "tool" || role === "tool_result"
    readonly property bool isError: role === "error"
    readonly property bool mono: isTool || isError
    readonly property color tone: isError ? root.urgent : (isTool ? root.muted : root.foreground)
    readonly property string displayText: meta.length > 0 ? text + "  ·  " + meta : text

    width: transcriptList.width
    height: column.implicitHeight + root.gap * 0.75

    Column {
      id: column
      width: parent.width
      spacing: Style.spacing.xs

      Text {
        text: root.roleLabel(row.role)
        color: row.isError ? root.urgent : root.muted
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.letterSpacing: 0.6
      }

      Rectangle {
        width: parent.width
        height: body.implicitHeight + (row.isUser ? root.gap * 1.5 : 0)
        radius: root.cornerRadius
        color: row.isUser ? root.surface : "transparent"
        border.width: row.isTool ? 1 : 0
        border.color: root.surface

        Text {
          id: body
          x: row.isUser ? root.gap : 0
          y: row.isUser ? root.gap * 0.75 : 0
          width: parent.width - (row.isUser ? root.gap * 2 : 0)
          text: row.displayText
          textFormat: Text.PlainText
          wrapMode: Text.Wrap
          color: row.tone
          font.family: row.mono ? root.monoFamily : root.fontFamily
          font.pixelSize: row.mono ? Style.font.bodySmall : Style.font.body
          lineHeight: 1.15
        }
      }
    }
  }

  component ApprovalCard: Rectangle {
    id: card
    required property int rowIndex
    required property string tool
    required property string danger
    required property string reason
    required property string capabilities
    required property string preview
    required property string argsJson
    required property string status

    readonly property bool pending: status === "pending"
    readonly property color tone: danger === "high" ? root.urgent
      : (danger === "medium" ? "#d7a45b" : root.accent)
    readonly property string detail: preview.length > 0 ? preview : argsJson

    width: parent ? parent.width : 0
    height: content.implicitHeight + root.gap * 2
    radius: root.cornerRadius
    color: root.surface
    border.width: 1
    border.color: card.tone

    Column {
      id: content
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: root.gap
      spacing: Style.spacing.xs

      Row {
        spacing: Style.spacing.sm

        Text {
          text: card.tool
          color: root.foreground
          font.family: root.monoFamily
          font.pixelSize: Style.font.body
          font.weight: Font.DemiBold
        }

        Rectangle {
          visible: card.danger === "high" || card.danger === "medium"
          width: dangerText.implicitWidth + Style.spacing.sm * 2
          height: dangerText.implicitHeight + Style.spacing.xs
          radius: height / 2
          color: "transparent"
          border.width: 1
          border.color: card.tone

          Text {
            id: dangerText
            anchors.centerIn: parent
            text: card.danger === "high" ? "HIGH RISK" : "NEEDS APPROVAL"
            color: card.tone
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Text {
        width: parent.width
        text: card.reason
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.muted
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        width: parent.width
        visible: card.capabilities.length > 0
        text: "capabilities: " + card.capabilities
        color: root.muted
        font.family: root.monoFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        width: parent.width
        visible: card.detail.length > 0
        text: card.detail
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        maximumLineCount: 7
        elide: Text.ElideRight
        color: root.foreground
        font.family: root.monoFamily
        font.pixelSize: Style.font.caption
        lineHeight: 1.1
      }

      Row {
        spacing: Style.spacing.sm
        visible: card.pending

        PillButton {
          label: "Allow once"
          tint: root.accent
          filled: true
          onClicked: root.decide(card.rowIndex, true, "once")
        }

        PillButton {
          label: "Allow session"
          tint: root.accent
          visible: card.danger !== "high"
          onClicked: root.decide(card.rowIndex, true, "session")
        }

        PillButton {
          label: "Deny"
          tint: root.urgent
          onClicked: root.decide(card.rowIndex, false, "once")
        }
      }

      Row {
        spacing: Style.spacing.sm
        visible: !card.pending

        Text {
          text: card.status === "approved" ? "Approved"
            : (card.status === "denied" ? "Denied" : "Expired")
          color: card.status === "approved" ? root.accent : root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          text: "dismiss"
          color: root.muted
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.underline: dismissMouse.containsMouse

          MouseArea {
            id: dismissMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.removeApproval(card.rowIndex)
          }
        }
      }
    }
  }

  // --------------------------------------------------------------------- panel
  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-omni"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      anchors.verticalCenterOffset: -Style.space(36)
      color: root.background
      borderSpec: root.borderSpec
      padding: root.pad

      MouseArea {
        anchors.fill: parent
        onClicked: input.forceActiveFocus()
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.gap

        // ------------------------------------------------------------- header
        Item {
          Layout.fillWidth: true
          Layout.preferredHeight: root.headerHeight

          Column {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: 0

            Text {
              text: "Omni"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              font.weight: Font.DemiBold
            }

            Text {
              text: "permissioned local agent"
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.sm

            Rectangle {
              width: statusRow.implicitWidth + Style.spacing.md * 2
              height: statusRow.implicitHeight + Style.spacing.xs * 2
              radius: height / 2
              color: root.surface

              Row {
                id: statusRow
                anchors.centerIn: parent
                spacing: Style.spacing.xs

                Rectangle {
                  width: 6
                  height: 6
                  radius: 3
                  color: root.statusTone()
                  anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                  text: root.statusLabel()
                  color: root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            PillButton {
              label: "Clear"
              visible: transcript.count > 0 && !root.busy
              onClicked: root.clearTranscript()
            }

            PillButton {
              label: "Esc"
              onClicked: root.dismiss()
            }
          }
        }

        // --------------------------------------------------------- transcript
        Item {
          Layout.fillWidth: true
          Layout.fillHeight: true

          ListView {
            id: transcriptList
            anchors.fill: parent
            clip: true
            model: transcript
            spacing: root.gap * 0.5
            boundsBehavior: Flickable.StopAtBounds
            delegate: MessageRow {}
            onCountChanged: root.scrollToEnd()
            onContentHeightChanged: root.scrollToEnd()
          }

          Column {
            anchors.centerIn: parent
            width: Math.min(parent.width, Style.space(430))
            spacing: root.gap
            visible: transcript.count === 0

            Text {
              width: parent.width
              text: "Ask Omni to inspect this machine, run a command, or walk you through a driver install."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              horizontalAlignment: Text.AlignHCenter
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              lineHeight: 1.2
            }

            Repeater {
              model: [
                "Check for USB devices",
                "How much disk space is left?",
                "Show my recent audit events"
              ]

              delegate: Rectangle {
                width: parent.width
                height: exampleText.implicitHeight + Style.spacing.md
                radius: root.cornerRadius
                color: exampleMouse.containsMouse ? root.surface : "transparent"
                border.width: 1
                border.color: root.surface

                Text {
                  id: exampleText
                  anchors.centerIn: parent
                  width: parent.width - root.gap * 2
                  text: modelData
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  horizontalAlignment: Text.AlignHCenter
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                MouseArea {
                  id: exampleMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: {
                    input.text = modelData
                    input.forceActiveFocus()
                  }
                }
              }
            }
          }
        }

        // ---------------------------------------------------------- approvals
        Item {
          id: approvalsSection
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? Math.min(approvalColumn.implicitHeight, Style.space(300)) : 0
          visible: approvals.count > 0

          Flickable {
            anchors.fill: parent
            contentWidth: width
            contentHeight: approvalColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Column {
              id: approvalColumn
              width: parent.width
              spacing: root.gap

              Repeater {
                model: approvals

                delegate: ApprovalCard {
                  rowIndex: index
                  tool: model.tool
                  danger: model.danger
                  reason: model.reason
                  capabilities: model.capabilities
                  preview: model.preview
                  argsJson: model.argsJson
                  status: model.status
                }
              }
            }
          }
        }

        // ------------------------------------------------------------- status
        Item {
          id: statusSection
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? Style.font.bodySmall + 4 : 0
          visible: root.busy || root.statusText.length > 0

          Row {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.sm

            Rectangle {
              width: 6
              height: 6
              radius: 3
              color: root.accent
              anchors.verticalCenter: parent.verticalCenter

              SequentialAnimation on opacity {
                running: root.busy
                loops: Animation.Infinite
                NumberAnimation { from: 1.0; to: 0.25; duration: 500 }
                NumberAnimation { from: 0.25; to: 1.0; duration: 500 }
              }
            }

            Text {
              text: (root.statusText.length > 0 ? root.statusText : "thinking")
                    + (root.busy && root.elapsedSeconds > 0 ? "  ·  " + root.elapsedSeconds + "s" : "")
              color: root.muted
              font.family: root.monoFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          PillButton {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            label: "Stop"
            tint: root.urgent
            visible: root.busy
            onClicked: root.stop()
          }
        }

        // -------------------------------------------------------------- input
        Rectangle {
          id: inputBox
          Layout.fillWidth: true
          Layout.preferredHeight: Math.max(root.inputMinHeight, input.height + root.gap * 2)
          radius: root.cornerRadius
          color: root.surface
          border.width: 1
          border.color: input.activeFocus ? root.accent : root.borderColor

          Text {
            visible: input.text.length === 0
            anchors.left: input.left
            anchors.right: input.right
            anchors.verticalCenter: input.verticalCenter
            text: "Ask Omni…  (Enter to send, Shift+Enter for a newline)"
            color: root.muted
            elide: Text.ElideRight
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          TextEdit {
            id: input
            anchors.left: parent.left
            anchors.leftMargin: root.gap
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - root.gap * 3 - sendButton.width
            height: Math.max(Style.font.body + 4, Math.min(root.inputMaxHeight - root.gap * 2, contentHeight))
            textFormat: TextEdit.PlainText
            wrapMode: TextEdit.Wrap
            selectByMouse: true
            color: root.foreground
            selectionColor: root.accent
            selectedTextColor: root.background
            font.family: root.fontFamily
            font.pixelSize: Style.font.body

            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                if (event.modifiers & Qt.ShiftModifier) {
                  event.accepted = false
                } else {
                  root.submit()
                  event.accepted = true
                }
              }
            }
          }

          PillButton {
            id: sendButton
            anchors.right: parent.right
            anchors.rightMargin: root.gap
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(76)
            label: root.busy ? "Stop" : "Send"
            tint: root.busy ? root.urgent : root.accent
            filled: true
            enabled: root.busy || input.text.trim().length > 0
            onClicked: root.busy ? root.stop() : root.submit()
          }
        }

        // ------------------------------------------------------------- footer
        Item {
          Layout.fillWidth: true
          Layout.preferredHeight: Style.font.caption + 2

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Enter send · Shift+Enter newline · Esc close"
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.xs

            Rectangle {
              width: 6
              height: 6
              radius: 3
              color: root.statusTone()
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              text: "daemon " + root.statusLabel()
              color: root.muted
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
