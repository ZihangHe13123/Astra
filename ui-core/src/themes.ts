/** The themes both clients draw from: colours, wording and marks. Reading and saving the
 * chosen theme stays with each client. */
export type ThemeName =
  | "hermes"
  | "glitchcity"
  | "classic"
  | "nord"
  | "dracula"
  | "solarized"
  | "gruvbox"
  | "lyra"
  | "moonlit"
  | "phosphor"
  | "obsidian";

export type ThemeChrome = {
  frameStyle?: "single" | "round" | "double";
  headerFrameStyle?: "single" | "round" | "double";
  activityFrameStyle?: "single" | "round" | "double";
  inputFrameStyle?: "single" | "round" | "double";
  brand?: string;
  headerTag?: string;
  exitLabel?: string;
  activeToolsTitle?: string;
  activeToolIcon?: string;
  detailTitle?: string;
  promptLabel?: string;
  promptPlaceholder?: string;
  menuMarker?: string;
  memoryTitle?: string;
  memoryGoalLabel?: string;
  memoryProgressLabel?: string;
  statusPrefix?: string;
  separator?: string;
  rolePrefixes?: Partial<Record<"user" | "assistant" | "reasoning" | "tool" | "error" | "system", string>>;
};

export type ThemeConsole = {
  bootTitle: string;
  bootTag: string;
  consoleTitle: string;
  consoleTag: string;
  nodeLabel: string;
  runtimeTitle: string;
  commandTitle: string;
  bootActive: string;
  readyTitle: string;
  readyStatus: string;
  handoff: string;
  waiting: string;
  systemsLabel: string;
  phases: readonly string[];
  mark: readonly string[];
  markPalette?: Record<string, ThemeColorKey>;
  toolState: string;
  skillState: string;
  mcpState: string;
  mcpWarningState: string;
  footerWide: string;
  footerStandard: string;
  footerCompact: string;
};

export type ThemeColorKey =
  | "text"
  | "muted"
  | "subtle"
  | "accent"
  | "accentAlt"
  | "emphasis"
  | "header"
  | "code"
  | "success"
  | "warning"
  | "danger"
  | "border";

export type UiTheme = {
  name: ThemeName;
  label: string;
  description: string;
  text: string;
  muted: string;
  subtle: string;
  accent: string;
  accentAlt: string;
  emphasis: string;
  header: string;
  code: string;
  codeBackground?: string;
  codeBlock: string;
  codeBlockBackground?: string;
  success: string;
  warning: string;
  danger: string;
  border: string;
  statusBackground?: string;
  menuBackground?: string;
  menuSelectedBackground?: string;
  strongBold: boolean;
  strongUsesBaseColor: boolean;
  codeDelimiters: boolean;
  prefixBold: boolean;
  menuInverse: boolean;
  chrome?: ThemeChrome;
  console: ThemeConsole;
};

export const THEME_CONSOLES: Record<ThemeName, ThemeConsole> = {
  hermes: {
    bootTitle: "HERMES // RELAY GATE",
    bootTag: "OLYMPUS UPLINK · WING 01",
    consoleTitle: "HERMES // MESSENGER CONSOLE",
    consoleTag: "DIVINE RELAY · ROUTE 01",
    nodeLabel: "CADUCEUS / GOLD NODE",
    runtimeTitle: "RELAY MANIFEST",
    commandTitle: "MESSENGER ROUTES",
    bootActive: "WINGED RELAY AWAKENING",
    readyTitle: "MESSAGE PATH OPEN",
    readyStatus: "RELAY NETWORK ONLINE",
    handoff: "DESCENT // MESSENGER CONSOLE",
    waiting: "AWAITING OLYMPUS CARRIER",
    systemsLabel: "SCROLLS · TALISMANS · PORTALS",
    phases: ["WAKING GOLD RELAY", "MOUNTING MEMORY SCROLLS", "CATALOGING TALISMANS", "OPENING MCP PORTALS"],
    mark: ["      __||__", "   __/  ||  \\__", "  <__   ||   __>", "     \\  ||  /", "      \\_||_/"],
    toolState: "ARMED",
    skillState: "CATALOGED",
    mcpState: "CONNECTED",
    mcpWarningState: "VEILED",
    footerWide: "MEMORY SCROLLS // SEALED   ·   TOOL RELAY // ARMED   ·   MESSAGE PATH // OPEN",
    footerStandard: "SCROLLS // SEALED · RELAY // ARMED · PATH // OPEN",
    footerCompact: "SCROLLS SEALED · RELAY ARMED · PATH OPEN",
  },
  glitchcity: {
    bootTitle: "ASTRA // BOOT CONSOLE",
    bootTag: "COLD START · CH-07",
    consoleTitle: "ASTRA // OPERATOR CONSOLE",
    consoleTag: "LOCAL LINK · CHANNEL 07",
    nodeLabel: "SYS-A7 / LOCAL NODE",
    runtimeTitle: "SYSTEM MATRIX",
    commandTitle: "COMMAND ROUTER",
    bootActive: "BOOT SEQUENCE ACTIVE",
    readyTitle: "LINK ESTABLISHED",
    readyStatus: "RUNTIME BUS ONLINE",
    handoff: "HANDOFF // OPERATOR CONSOLE",
    waiting: "LOCAL LINK · WAITING FOR SUBSYSTEMS",
    systemsLabel: "MEMORY · TOOLS · MCP",
    phases: ["MODEL BUS HANDSHAKE", "MEMORY CORE MOUNT", "TOOL REGISTRY SCAN", "MCP LINK NEGOTIATION"],
    mark: ["       /\\", "      /  \\", "     / /\\ \\", "    / /__\\ \\", "   /_/    \\_\\"],
    toolState: "ONLINE",
    skillState: "INDEXED",
    mcpState: "LINKED",
    mcpWarningState: "DEGRADED",
    footerWide: "CORE MEMORY // READY   ·   TOOL BUS // ONLINE   ·   INPUT CHANNEL // OPEN",
    footerStandard: "MEMORY // READY · TOOLS // ONLINE · INPUT // OPEN",
    footerCompact: "MEM READY · TOOLS ONLINE · INPUT OPEN",
  },
  classic: {
    bootTitle: "AGENT SYSTEM BIOS",
    bootTag: "POST · REV 1.0",
    consoleTitle: "AGENT SYSTEM // MAIN CONSOLE",
    consoleTag: "TTY0 · LOCALHOST",
    nodeLabel: "ASTRA-PC / CONSOLE",
    runtimeTitle: "DEVICE STATUS",
    commandTitle: "AVAILABLE COMMANDS",
    bootActive: "POWER-ON SELF TEST",
    readyTitle: "POST COMPLETE",
    readyStatus: "SYSTEM READY",
    handoff: "BOOT // MAIN CONSOLE",
    waiting: "CHECKING INSTALLED DEVICES",
    systemsLabel: "MEM · TOOLS · NETWORK",
    phases: ["CHECKING MODEL DEVICE", "TESTING MEMORY BANK", "LOADING TOOL DRIVERS", "PROBING MCP NETWORK"],
    mark: ["     +------+ ", "     | ASTRA| ", "     |  >_  | ", "     +------+ ", "       ||||   "],
    toolState: "OK",
    skillState: "OK",
    mcpState: "OK",
    mcpWarningState: "WARN",
    footerWide: "MEMORY OK   ·   TOOL DRIVERS OK   ·   KEYBOARD READY   ·   PRESS /help",
    footerStandard: "MEM OK · TOOLS OK · INPUT READY · /help",
    footerCompact: "MEM OK · TOOLS OK · INPUT READY",
  },
  nord: {
    bootTitle: "NORD // POLAR LINK",
    bootTag: "FROST NODE · N-01",
    consoleTitle: "NORD // ARCTIC STATION",
    consoleTag: "AURORA LINK · NORTH 01",
    nodeLabel: "POLAR NODE / N-01",
    runtimeTitle: "ICEFIELD ARRAY",
    commandTitle: "EXPEDITION ROUTES",
    bootActive: "POLAR SYSTEMS WAKING",
    readyTitle: "AURORA LINK STABLE",
    readyStatus: "STATION SYSTEMS ONLINE",
    handoff: "TRANSFER // ARCTIC STATION",
    waiting: "LISTENING THROUGH THE WHITEOUT",
    systemsLabel: "ARCHIVE · MODULES · BEACONS",
    phases: ["CALIBRATING MODEL COMPASS", "THAWING MEMORY ARCHIVE", "MAPPING TOOL MODULES", "PINGING MCP BEACONS"],
    mark: ["        /\\", "    /\\ /  \\ /\\", "   /  V /\\ V  \\", "  /____/  \\____\\", "      NORD NODE"],
    toolState: "READY",
    skillState: "MAPPED",
    mcpState: "BEACON",
    mcpWarningState: "WHITEOUT",
    footerWide: "MEMORY ARCHIVE // THAWED   ·   TOOL SLED // READY   ·   AURORA CHANNEL // OPEN",
    footerStandard: "ARCHIVE // READY · TOOLS // MAPPED · LINK // OPEN",
    footerCompact: "ARCHIVE READY · TOOLS MAPPED · LINK OPEN",
  },
  dracula: {
    bootTitle: "DRACULA // NIGHT RELAY",
    bootTag: "CASTLE LINK · MIDNIGHT",
    consoleTitle: "DRACULA // NOCTURNE CONSOLE",
    consoleTag: "CRYPT RELAY · TOWER 13",
    nodeLabel: "NIGHT NODE / XIII",
    runtimeTitle: "COVEN MATRIX",
    commandTitle: "ARCANE INVOCATIONS",
    bootActive: "NIGHT SYSTEMS RISING",
    readyTitle: "THE RELAY AWAKENS",
    readyStatus: "CASTLE NETWORK ONLINE",
    handoff: "ENTER // NOCTURNE CONSOLE",
    waiting: "LISTENING BEYOND THE VEIL",
    systemsLabel: "MEMORY · RUNES · FAMILIARS",
    phases: ["SUMMONING MODEL FAMILIAR", "UNSEALING MEMORY CRYPT", "INSCRIBING TOOL RUNES", "OPENING MCP GATEWAYS"],
    mark: ["    /\\       /\\", "   /  \\.___./  \\", "  <    /   \\    >", "   \\__/\\_/\\__/", "      \\ V /"],
    toolState: "BOUND",
    skillState: "INSCRIBED",
    mcpState: "SUMMONED",
    mcpWarningState: "CURSED",
    footerWide: "MEMORY CRYPT // UNSEALED   ·   TOOL RUNES // BOUND   ·   NIGHT CHANNEL // OPEN",
    footerStandard: "CRYPT // OPEN · RUNES // BOUND · CHANNEL // OPEN",
    footerCompact: "CRYPT OPEN · RUNES BOUND · CHANNEL OPEN",
  },
  solarized: {
    bootTitle: "SOLAR // HELIOGRAPH",
    bootTag: "DAWN ARRAY · S-03",
    consoleTitle: "SOLARIZED // OBSERVATORY",
    consoleTag: "DAYLIGHT BUS · ORBIT 03",
    nodeLabel: "HELIO NODE / S-03",
    runtimeTitle: "ORBITAL ARRAY",
    commandTitle: "OBSERVATION ROUTES",
    bootActive: "DAWN CALIBRATION ACTIVE",
    readyTitle: "SUNLINE ACQUIRED",
    readyStatus: "OBSERVATORY ONLINE",
    handoff: "FOCUS // OBSERVATORY",
    waiting: "TRACKING FIRST LIGHT",
    systemsLabel: "EPHEMERIS · LENSES · BEACONS",
    phases: ["ALIGNING MODEL TELESCOPE", "LOADING MEMORY EPHEMERIS", "CALIBRATING TOOL LENSES", "ACQUIRING MCP BEACONS"],
    mark: ["        \\ | /", "      --  *  --", "        / | \\", "     .- SOL -.", "       ORBIT 03"],
    toolState: "FOCUSED",
    skillState: "CHARTED",
    mcpState: "ACQUIRED",
    mcpWarningState: "ECLIPSED",
    footerWide: "MEMORY EPHEMERIS // LOADED   ·   TOOL ARRAY // FOCUSED   ·   DAYLIGHT CHANNEL // OPEN",
    footerStandard: "EPHEMERIS // READY · ARRAY // FOCUSED · LINK // OPEN",
    footerCompact: "MAP READY · ARRAY FOCUSED · LINK OPEN",
  },
  gruvbox: {
    bootTitle: "GRUVBOX // CRT WARMUP",
    bootTag: "PHOSPHOR BUS · G-08",
    consoleTitle: "GRUVBOX // ANALOG WORKBENCH",
    consoleTag: "CRT LINK · BENCH 08",
    nodeLabel: "PHOSPHOR NODE / G-08",
    runtimeTitle: "WORKBENCH METERS",
    commandTitle: "PATCH BAY",
    bootActive: "TUBES WARMING",
    readyTitle: "PHOSPHOR LOCKED",
    readyStatus: "WORKBENCH ONLINE",
    handoff: "PATCH // ANALOG WORKBENCH",
    waiting: "WAITING FOR CARRIER TONE",
    systemsLabel: "TAPE · MODULES · PATCHES",
    phases: ["TUNING MODEL OSCILLATOR", "THREADING MEMORY TAPE", "WARMING TOOL MODULES", "PATCHING MCP CHANNELS"],
    mark: ["     .--------.", "    /  >_     /|", "   +--------+ |", "   | CRT-08 | /", "   +--------+'"],
    toolState: "HOT",
    skillState: "RACKED",
    mcpState: "PATCHED",
    mcpWarningState: "NO SIGNAL",
    footerWide: "MEMORY TAPE // THREADED   ·   TOOL RACK // HOT   ·   INPUT PATCH // OPEN",
    footerStandard: "TAPE // READY · RACK // HOT · PATCH // OPEN",
    footerCompact: "TAPE READY · RACK HOT · PATCH OPEN",
  },
  lyra: {
    bootTitle: "LYRA // NIGHT TERMINAL",
    bootTag: "LATE SHIFT · CH-31",
    consoleTitle: "LYRA // CRIMSON CONSOLE",
    consoleTag: "RED EYE LINK · CHANNEL 31",
    nodeLabel: "LYRA / CRIMSON NODE",
    runtimeTitle: "NIGHT WATCH ROSTER",
    commandTitle: "CLAW COMMANDS",
    bootActive: "PAWS STRETCHING",
    readyTitle: "RED EYES CALIBRATED",
    readyStatus: "NIGHT WATCH ONLINE",
    handoff: "WAKE // CRIMSON CONSOLE",
    waiting: "CAT NAP · TAIL FLICKING",
    systemsLabel: "BOXES · CLAWS · RED EYES",
    phases: ["STRETCHING PAWS", "CALIBRATING RED EYES", "SHARPENING DEBUG CLAWS", "OPENING BOX PORTALS"],
    mark: [
      "..█░█....█░█..",
      "..████..████..",
      ".████████████.",
      "██████████████",
      "███▒▒████▒▒███",
      "██████▄▄██████",
      ".████████████.",
      "..▀▀▀▀▀▀▀▀▀▀..",
    ],
    markPalette: { "█": "muted", "▄": "muted", "▀": "muted", "░": "emphasis", "▒": "accent" },
    toolState: "SHARP",
    skillState: "CATALOGED",
    mcpState: "PURRED",
    mcpWarningState: "BRISTLED",
    footerWide: "MEMORY BALLS // SAFE   ·   CLAWS // SHARP   ·   RED EYES // OPEN",
    footerStandard: "BALLS // SAFE · CLAWS // SHARP · EYES // OPEN",
    footerCompact: "BALLS SAFE · CLAWS SHARP · EYES OPEN",
  },
  moonlit: {
    bootTitle: "MOONLIT // PAPER CONSOLE",
    bootTag: "DAY SHIFT · FOLIO 01",
    consoleTitle: "MOONLIT // INK CONSOLE",
    consoleTag: "MORNING BUS · FOLIO 01",
    nodeLabel: "MOON / PAPER DESK",
    runtimeTitle: "MORNING PAGES",
    commandTitle: "INK ROUTES",
    bootActive: "LAMP WARMING",
    readyTitle: "INK SETTLED",
    readyStatus: "PAPER DESK ONLINE",
    handoff: "OPEN // INK CONSOLE",
    waiting: "INK DRYING · PAGE HELD",
    systemsLabel: "FOLIO · QUILL · LAMP",
    phases: ["UNFOLDING MEMORY FOLIO", "WARMING MORNING LAMP", "SHARPENING TOOL QUILL", "ALIGNING MCP MARGINS"],
    mark: ["     _.-.", "   .'   '", "  : MOON :", "  : LIT  :", "   '.__.'"],
    toolState: "INKED",
    skillState: "FOLDED",
    mcpState: "PAGED",
    mcpWarningState: "SMUDGED",
    footerWide: "MEMORY FOLIO // OPEN   ·   TOOL QUILL // INKED   ·   MORNING CHANNEL // STEADY",
    footerStandard: "FOLIO // OPEN · QUILL // INKED · CHANNEL // STEADY",
    footerCompact: "FOLIO OPEN · QUILL INKED · LINK STEADY",
  },
  phosphor: {
    bootTitle: "PHOSPHOR // WAVEFORM TERMINAL",
    bootTag: "GREEN SCREEN · TUBE 09",
    consoleTitle: "PHOSPHOR // WAVEFORM CONSOLE",
    consoleTag: "VGA LINK · TUBE 09",
    nodeLabel: "P1 / PHOSPHOR NODE",
    runtimeTitle: "SCANLINE ARRAY",
    commandTitle: "KEYBOARD ROUTES",
    bootActive: "FILAMENTS HEATING",
    readyTitle: "TRACE LOCKED",
    readyStatus: "GREEN SCREEN ONLINE",
    handoff: "SWITCH // WAVEFORM CONSOLE",
    waiting: "HOLDING VERTICAL SYNC",
    systemsLabel: "TAPE · TUBES · SCANLINES",
    phases: ["WARMING VACUUM TUBES", "CALIBRATING SCANLINES", "LOADING MEMORY TAPE", "TUNING MCP CARRIER"],
    mark: ["     __", "    /  \\_/\\__", "  ~_/\\     \\_/\\_", "   WAVEFORM P1", "   PHOSPHOR-09"],
    toolState: "GLOWING",
    skillState: "TAPED",
    mcpState: "TUNED",
    mcpWarningState: "DEGAUSSED",
    footerWide: "MEMORY TAPE // LOADED   ·   TUBES // WARM   ·   GREEN CHANNEL // LOCKED",
    footerStandard: "TAPE // LOADED · TUBES // WARM · CHANNEL // LOCKED",
    footerCompact: "TAPE LOADED · TUBES WARM · CHANNEL LOCKED",
  },
  obsidian: {
    bootTitle: "OBSIDIAN // VOID BOOT",
    bootTag: "NULL FIELD · BLACK 01",
    consoleTitle: "OBSIDIAN // MONOLITH",
    consoleTag: "VOID LINK · BLACK 01",
    nodeLabel: "VOID / MONOLITH",
    runtimeTitle: "SILENT ARRAY",
    commandTitle: "EDGES",
    bootActive: "SURFACE LEVELING",
    readyTitle: "VOID STABLE",
    readyStatus: "MONOLITH ONLINE",
    handoff: "ENTER // MONOLITH",
    waiting: "ABSOLUTE QUIET",
    systemsLabel: "VOID · EDGE · SIGNAL",
    phases: ["CLEARING THE VOID", "LEVELING THE SURFACE", "SHARPENING ONE EDGE", "OPENING A SIGNAL"],
    mark: ["      ______", "     /      /", "    /      /", "   /______/", "   MONOLITH"],
    toolState: "KEEN",
    skillState: "ETCHED",
    mcpState: "THREADED",
    mcpWarningState: "SILENCED",
    footerWide: "MEMORY VOID // STILL   ·   TOOL EDGE // KEEN   ·   SIGNAL // MINIMAL",
    footerStandard: "VOID // STILL · EDGE // KEEN · SIGNAL // MINIMAL",
    footerCompact: "VOID STILL · EDGE KEEN · SIGNAL MINIMAL",
  },
};

export const THEMES: Record<ThemeName, UiTheme> = {
  hermes: {
    name: "hermes",
    label: "Hermes Gold",
    description: "warm cream text, amber gold accents, bronze borders, and navy panels",
    text: "#FFF8DC",
    muted: "#C7A96B",
    subtle: "#8B6914",
    accent: "#FFBF00",
    accentAlt: "#DAA520",
    emphasis: "#FFD700",
    header: "#FFD700",
    code: "#FFCF40",
    codeBackground: "#1A1A2E",
    codeBlock: "#FFF8DC",
    codeBlockBackground: "#1A1A2E",
    success: "#4CAF50",
    warning: "#FFA726",
    danger: "#EF5350",
    border: "#CD7F32",
    statusBackground: "#1A1A2E",
    menuBackground: "#1A1A2E",
    menuSelectedBackground: "#333355",
    strongBold: true,
    strongUsesBaseColor: false,
    codeDelimiters: true,
    prefixBold: true,
    menuInverse: false,
    console: THEME_CONSOLES.hermes,
    chrome: {
      frameStyle: "round",
      headerFrameStyle: "round",
      activityFrameStyle: "round",
      inputFrameStyle: "round",
      brand: "HERMES // MESSENGER",
      headerTag: "OLYMPUS RELAY ONLINE",
      exitLabel: "end relay",
      activeToolsTitle: "MESSENGER RELAY // LIVE TOOLS",
      activeToolIcon: "✦",
      detailTitle: "ARCHIVE // DELIVERED RESULT",
      promptLabel: "dispatch",
      promptPlaceholder: "compose a message",
      menuMarker: "›",
      memoryTitle: "MESSAGE ROUTE / 工作计划",
      memoryGoalLabel: "DESTINATION / 目标  ",
      memoryProgressLabel: "JOURNEY / 进度  ",
      statusPrefix: "RELAY",
      separator: " · ",
      rolePrefixes: {
        user: "YOU  ⇢ ",
        assistant: "MSG  ⇢ ",
        reasoning: "OMEN ⇢ ",
        tool: "RELAY⇢ ",
        error: "ERR  ⇢ ",
        system: "NEWS ⇢ ",
      },
    },
  },
  glitchcity: {
    name: "glitchcity",
    label: "Glitch City After Dark",
    description: "late-night cyberpunk bar console with neon pink, teal, and amber",
    text: "#D8D4C8",
    muted: "#8A91A8",
    subtle: "#4B526D",
    accent: "#FF4FA3",
    accentAlt: "#42D9C8",
    emphasis: "#FFB84D",
    header: "#FFB84D",
    code: "#42D9C8",
    codeBackground: "#151A31",
    codeBlock: "#A7F3E8",
    codeBlockBackground: "#10152A",
    success: "#6FD08C",
    warning: "#F2C14E",
    danger: "#FF5C6C",
    border: "#8A3E72",
    statusBackground: "#10152A",
    menuBackground: "#10152A",
    menuSelectedBackground: "#4A2045",
    strongBold: true,
    strongUsesBaseColor: false,
    codeDelimiters: true,
    prefixBold: true,
    menuInverse: false,
    console: THEME_CONSOLES.glitchcity,
    chrome: {
      frameStyle: "double",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "double",
      brand: "GLITCH CITY // AGENT BAR",
      headerTag: "NIGHT SHIFT ONLINE",
      exitLabel: "close shift",
      activeToolsTitle: "MIXING // LIVE TOOLS",
      activeToolIcon: "◆",
      detailTitle: "ARCHIVE // TOOL RESULT",
      promptLabel: "order",
      promptPlaceholder: "type your order",
      menuMarker: "◆",
      memoryTitle: "SHIFT PLAN / 工作计划",
      memoryGoalLabel: "ORDER / 目标  ",
      memoryProgressLabel: "STATUS / 进度  ",
      statusPrefix: "SHIFT",
      separator: " │ ",
      rolePrefixes: {
        user: "YOU  › ",
        assistant: "LYRA › ",
        reasoning: "THINK› ",
        tool: "TOOL › ",
        error: "ERR  › ",
        system: "SYS  › ",
      },
    },
  },
  classic: {
    name: "classic",
    label: "Classic",
    description: "original bright ANSI colors and highlighted inline code",
    text: "white", muted: "gray", subtle: "gray", accent: "cyanBright", accentAlt: "blueBright",
    emphasis: "white", header: "magentaBright", code: "yellowBright", codeBackground: "gray",
    codeBlock: "greenBright", codeBlockBackground: "black",
    success: "greenBright", warning: "yellow", danger: "redBright", border: "gray",
    strongBold: true, strongUsesBaseColor: true, codeDelimiters: false, prefixBold: true, menuInverse: true,
    console: THEME_CONSOLES.classic,
    chrome: {
      frameStyle: "single",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "single",
      brand: "AGENT SYSTEM",
      headerTag: "READY",
      exitLabel: "exit",
      activeToolsTitle: "RUNNING PROGRAMS",
      activeToolIcon: ">",
      detailTitle: "PROGRAM OUTPUT",
      promptLabel: "C:\\>",
      promptPlaceholder: "type a command",
      menuMarker: ">",
      memoryTitle: "TASK LIST / 工作计划",
      memoryGoalLabel: "GOAL / 目标  ",
      memoryProgressLabel: "STATUS / 进度  ",
      statusPrefix: "SYS",
      separator: " | ",
      rolePrefixes: {
        user: "YOU> ",
        assistant: "AI>  ",
        reasoning: "DBG> ",
        tool: "RUN> ",
        error: "ERR> ",
        system: "SYS> ",
      },
    },
  },
  nord: {
    name: "nord",
    label: "Nord",
    description: "cool low-glare blue-gray",
    text: "#D8DEE9", muted: "#6F7787", subtle: "#4C566A", accent: "#88C0D0", accentAlt: "#81A1C1",
    emphasis: "#A3BE8C", header: "#B48EAD", code: "#8FBCBB", codeBlock: "#8FBCBB",
    success: "#A3BE8C", warning: "#D9B86C", danger: "#BF616A", border: "#4C566A",
    strongBold: false, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: false, menuInverse: false,
    console: THEME_CONSOLES.nord,
    chrome: {
      frameStyle: "round",
      headerFrameStyle: "round",
      activityFrameStyle: "round",
      inputFrameStyle: "round",
      brand: "NORD // POLAR STATION",
      headerTag: "AURORA LINK",
      exitLabel: "leave station",
      activeToolsTitle: "EXPEDITION // LIVE MODULES",
      activeToolIcon: "◇",
      detailTitle: "ICE ARCHIVE // RESULT",
      promptLabel: "signal",
      promptPlaceholder: "send across the ice",
      menuMarker: "◇",
      memoryTitle: "EXPEDITION PLAN / 工作计划",
      memoryGoalLabel: "BEARING / 目标  ",
      memoryProgressLabel: "TRAIL / 进度  ",
      statusPrefix: "NORD",
      separator: " · ",
      rolePrefixes: {
        user: "YOU  ◇ ",
        assistant: "NORD ◇ ",
        reasoning: "TRACE◇ ",
        tool: "MOD  ◇ ",
        error: "WARN ◇ ",
        system: "BASE ◇ ",
      },
    },
  },
  dracula: {
    name: "dracula",
    label: "Dracula",
    description: "dark purple with soft pink and cyan accents",
    text: "#F8F8F2", muted: "#6272A4", subtle: "#44475A", accent: "#8BE9FD", accentAlt: "#BD93F9",
    emphasis: "#50FA7B", header: "#FF79C6", code: "#F1FA8C", codeBlock: "#50FA7B",
    success: "#50FA7B", warning: "#FFB86C", danger: "#FF5555", border: "#44475A",
    strongBold: false, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: false, menuInverse: false,
    console: THEME_CONSOLES.dracula,
    chrome: {
      frameStyle: "double",
      headerFrameStyle: "double",
      activityFrameStyle: "single",
      inputFrameStyle: "double",
      brand: "DRACULA // NIGHT RELAY",
      headerTag: "CASTLE LINK",
      exitLabel: "seal relay",
      activeToolsTitle: "ARCANE WORK // LIVE RUNES",
      activeToolIcon: "◆",
      detailTitle: "CRYPT ARCHIVE // RESULT",
      promptLabel: "invoke",
      promptPlaceholder: "whisper into the night",
      menuMarker: "◆",
      memoryTitle: "COVEN PLAN / 工作计划",
      memoryGoalLabel: "OATH / 目标  ",
      memoryProgressLabel: "RITE / 进度  ",
      statusPrefix: "NIGHT",
      separator: " † ",
      rolePrefixes: {
        user: "YOU  † ",
        assistant: "NIGHT† ",
        reasoning: "VEIL † ",
        tool: "RUNE † ",
        error: "CURSE† ",
        system: "CRYPT† ",
      },
    },
  },
  solarized: {
    name: "solarized",
    label: "Solarized Dark",
    description: "very low-contrast blue-green for long sessions",
    text: "#93A1A1", muted: "#657B83", subtle: "#073642", accent: "#2AA198", accentAlt: "#268BD2",
    emphasis: "#859900", header: "#6C71C4", code: "#B58900", codeBlock: "#2AA198",
    success: "#859900", warning: "#B58900", danger: "#DC322F", border: "#586E75",
    strongBold: false, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: false, menuInverse: false,
    console: THEME_CONSOLES.solarized,
    chrome: {
      frameStyle: "round",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "round",
      brand: "SOLARIZED // OBSERVATORY",
      headerTag: "DAYLIGHT BUS",
      exitLabel: "close observatory",
      activeToolsTitle: "OBSERVATION // LIVE ARRAY",
      activeToolIcon: "⊙",
      detailTitle: "EPHEMERIS // RESULT",
      promptLabel: "observe",
      promptPlaceholder: "enter an observation",
      menuMarker: "⊙",
      memoryTitle: "OBSERVATION PLAN / 工作计划",
      memoryGoalLabel: "TARGET / 目标  ",
      memoryProgressLabel: "ORBIT / 进度  ",
      statusPrefix: "SOL",
      separator: " · ",
      rolePrefixes: {
        user: "YOU  ⊙ ",
        assistant: "SOL  ⊙ ",
        reasoning: "ORBIT⊙ ",
        tool: "LENS ⊙ ",
        error: "FLARE⊙ ",
        system: "DAWN ⊙ ",
      },
    },
  },
  gruvbox: {
    name: "gruvbox",
    label: "Gruvbox",
    description: "warm muted amber and aqua",
    text: "#EBDBB2", muted: "#928374", subtle: "#504945", accent: "#83A598", accentAlt: "#8EC07C",
    emphasis: "#B8BB26", header: "#D3869B", code: "#FABD2F", codeBlock: "#8EC07C",
    success: "#B8BB26", warning: "#D79921", danger: "#FB4934", border: "#665C54",
    strongBold: false, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: false, menuInverse: false,
    console: THEME_CONSOLES.gruvbox,
    chrome: {
      frameStyle: "single",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "double",
      brand: "GRUVBOX // ANALOG WORKSHOP",
      headerTag: "PHOSPHOR ONLINE",
      exitLabel: "power down",
      activeToolsTitle: "WORKBENCH // LIVE MODULES",
      activeToolIcon: "■",
      detailTitle: "TAPE LOG // RESULT",
      promptLabel: "patch",
      promptPlaceholder: "route a command",
      menuMarker: "■",
      memoryTitle: "BENCH PLAN / 工作计划",
      memoryGoalLabel: "BUILD / 目标  ",
      memoryProgressLabel: "METER / 进度  ",
      statusPrefix: "CRT",
      separator: " :: ",
      rolePrefixes: {
        user: "YOU :: ",
        assistant: "CRT :: ",
        reasoning: "TAPE:: ",
        tool: "PATCH: ",
        error: "FAULT: ",
        system: "BENCH: ",
      },
    },
  },
  lyra: {
    name: "lyra",
    label: "Lyra Night",
    description: "ink-black night console with crimson eyes and moon-gold accents",
    text: "#E8E3E6", muted: "#90878D", subtle: "#3D353A", accent: "#E5484D", accentAlt: "#F2C078",
    emphasis: "#FF8FA3", header: "#FF8FA3", code: "#F2C078", codeBlock: "#E8E3E6",
    success: "#F2C078", warning: "#E0AF68", danger: "#FF6B4A", border: "#3D353A",
    codeBackground: "#1A1214", codeBlockBackground: "#1A1214",
    statusBackground: "#1A1214", menuBackground: "#1A1214", menuSelectedBackground: "#3A1F24",
    strongBold: true, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: true, menuInverse: false,
    console: THEME_CONSOLES.lyra,
    chrome: {
      frameStyle: "round",
      headerFrameStyle: "round",
      activityFrameStyle: "round",
      inputFrameStyle: "round",
      brand: "LYRA",
      headerTag: "NIGHT WATCH ONLINE",
      exitLabel: "end watch",
      activeToolsTitle: "NIGHT WATCH // LIVE TOOLS",
      activeToolIcon: "ฅ",
      detailTitle: "ARCHIVE // DELIVERED",
      promptLabel: "lyra",
      promptPlaceholder: "talk to the cat",
      menuMarker: "▸",
      memoryTitle: "NIGHT LOG / 工作计划",
      memoryGoalLabel: "WISH / 目标  ",
      memoryProgressLabel: "PAWS / 进度  ",
      statusPrefix: "LYRA",
      separator: " · ",
      rolePrefixes: {
        user: "HUMAN⇢ ",
        assistant: "LYRA ⇢ ",
        reasoning: "THINK⇢ ",
        tool: "CLAW ⇢ ",
        error: "HISS ⇢ ",
        system: "MEOW ⇢ ",
      },
    },
  },
  moonlit: {
    name: "moonlit",
    label: "Moonlit Paper",
    description: "warm paper-white daylight theme with deep crimson ink",
    text: "#2E2A2C", muted: "#8C8489", subtle: "#C4BCB3", accent: "#C73E5A", accentAlt: "#B4762A",
    emphasis: "#A83A4F", header: "#A83A4F", code: "#B4762A", codeBlock: "#2E2A2C",
    success: "#2E7D46", warning: "#9A6B15", danger: "#C0392B", border: "#D8D1C9",
    codeBackground: "#F1ECE4", codeBlockBackground: "#F1ECE4",
    statusBackground: "#F1ECE4", menuBackground: "#F1ECE4", menuSelectedBackground: "#E9E2D8",
    strongBold: true, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: true, menuInverse: false,
    console: THEME_CONSOLES.moonlit,
    chrome: {
      frameStyle: "single",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "single",
      brand: "月白 // MOONLIT",
      headerTag: "DAYLIGHT DESK",
      exitLabel: "close folio",
      activeToolsTitle: "MORNING DESK // LIVE TOOLS",
      activeToolIcon: "✎",
      detailTitle: "ARCHIVE // PAGE",
      promptLabel: "note",
      promptPlaceholder: "write on the page",
      menuMarker: "·",
      memoryTitle: "MORNING PAGES / 工作计划",
      memoryGoalLabel: "INTENT / 目标  ",
      memoryProgressLabel: "PAGES / 进度  ",
      statusPrefix: "MOON",
      separator: " · ",
      rolePrefixes: {
        user: "READ ⇢ ",
        assistant: "INK  ⇢ ",
        reasoning: "DRAFT⇢ ",
        tool: "QUILL⇢ ",
        error: "BLUR ⇢ ",
        system: "PAGE ⇢ ",
      },
    },
  },
  phosphor: {
    name: "phosphor",
    label: "Phosphor Green",
    description: "classic green CRT terminal with amber secondary channel",
    text: "#9CE8A8", muted: "#5A8A62", subtle: "#1E3324", accent: "#46F58C", accentAlt: "#FFB000",
    emphasis: "#C8FFD9", header: "#C8FFD9", code: "#FFB000", codeBlock: "#9CE8A8",
    success: "#46F58C", warning: "#FFB000", danger: "#FF6259", border: "#2E5C3A",
    codeBackground: "#0A1A0F", codeBlockBackground: "#0A1A0F",
    statusBackground: "#0A1A0F", menuBackground: "#0A1A0F", menuSelectedBackground: "#1E4A2A",
    strongBold: true, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: true, menuInverse: false,
    console: THEME_CONSOLES.phosphor,
    chrome: {
      frameStyle: "double",
      headerFrameStyle: "double",
      activityFrameStyle: "single",
      inputFrameStyle: "double",
      brand: "PHOSPHOR // GREEN SCREEN",
      headerTag: "TUBE 09 · LOCKED",
      exitLabel: "power off",
      activeToolsTitle: "WAVEFORM // LIVE TOOLS",
      activeToolIcon: "∿",
      detailTitle: "PRINTOUT // LAST RESULT",
      promptLabel: "type",
      promptPlaceholder: "key into the green screen",
      menuMarker: "►",
      memoryTitle: "TAPE LOG / 工作计划",
      memoryGoalLabel: "TARGET / 目标  ",
      memoryProgressLabel: "TRACK / 进度  ",
      statusPrefix: "P1",
      separator: " · ",
      rolePrefixes: {
        user: "KEY  ⇢ ",
        assistant: "P1   ⇢ ",
        reasoning: "SCAN ⇢ ",
        tool: "BUS  ⇢ ",
        error: "FAULT⇢ ",
        system: "SYS  ⇢ ",
      },
    },
  },
  obsidian: {
    name: "obsidian",
    label: "Obsidian Void",
    description: "pure black OLED console, grayscale with a single electric-blue signal",
    text: "#F2F2F2", muted: "#7A7A7A", subtle: "#2A2A2A", accent: "#4DA3FF", accentAlt: "#9BA3AE",
    emphasis: "#FFFFFF", header: "#FFFFFF", code: "#E6E6E6", codeBlock: "#F2F2F2",
    success: "#56C271", warning: "#E6B455", danger: "#FF5C5C", border: "#2A2A2A",
    codeBackground: "#111111", codeBlockBackground: "#111111",
    statusBackground: "#111111", menuBackground: "#111111", menuSelectedBackground: "#1F1F1F",
    strongBold: true, strongUsesBaseColor: false, codeDelimiters: true, prefixBold: true, menuInverse: false,
    console: THEME_CONSOLES.obsidian,
    chrome: {
      frameStyle: "single",
      headerFrameStyle: "single",
      activityFrameStyle: "single",
      inputFrameStyle: "single",
      brand: "OBSIDIAN",
      headerTag: "VOID LINK",
      exitLabel: "back to void",
      activeToolsTitle: "ACTIVE TOOLS",
      activeToolIcon: "+",
      detailTitle: "OUTPUT",
      promptLabel: "cmd",
      promptPlaceholder: "input",
      menuMarker: ">",
      memoryTitle: "PLAN / 工作计划",
      memoryGoalLabel: "GOAL / 目标  ",
      memoryProgressLabel: "STEP / 进度  ",
      statusPrefix: "VOID",
      separator: " · ",
      rolePrefixes: {
        user: "IN   ⇢ ",
        assistant: "OUT  ⇢ ",
        reasoning: "CALC ⇢ ",
        tool: "EXEC ⇢ ",
        error: "ERR  ⇢ ",
        system: "SYS  ⇢ ",
      },
    },
  },
};

export const THEME_NAMES = Object.keys(THEMES) as ThemeName[];
export const DEFAULT_THEME: ThemeName = "hermes";

export type MarkSegment = { text: string; color: string };

export function markLineSegments(
  line: string,
  palette: Record<string, ThemeColorKey> | undefined,
  theme: UiTheme,
  fallbackColor: string,
): MarkSegment[] {
  if (!palette) return [{ text: line, color: fallbackColor }];
  const colorTable = theme as unknown as Record<ThemeColorKey, string>;
  const segments: MarkSegment[] = [];
  let i = 0;
  while (i < line.length) {
    const ch = line.charAt(i);
    let j = i + 1;
    while (j < line.length && line.charAt(j) === ch) j++;
    const key = palette[ch] ?? "accent";
    segments.push({ text: line.slice(i, j), color: colorTable[key] ?? fallbackColor });
    i = j;
  }
  return segments;
}

export function isThemeName(value: string): value is ThemeName {
  return THEME_NAMES.includes(value as ThemeName);
}
