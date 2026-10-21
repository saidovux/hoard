# Hoard implementation roadmap

Hoard is developed in nine bounded rounds. A round is complete only after local verification,
wheel and privacy review, normal fast-forward publication to `origin/main`, remote-HEAD
verification and an available GitHub Actions check.

1. **AI Core — completed.** Provider-neutral requests and responses, DeepSeek transport, JSON
   validation, privacy limits and the independent AI job pool.
2. **Textual TUI Foundation — completed.** Strict black theme, modular app/screens/widgets, Overview
   and Activity over existing IPC, responsive keyboard navigation and bounded live polling.
3. **Telegram Screens — completed.** Telegram authorization, collector controls, source
   validation/editing, targeted scans, archive/profile/job views and catalog operations are
   available through typed IPC without moving Telethon out of the daemon.
4. **Prompts, Chats & Memory.** Add durable, explicit user-owned AI prompt and conversation data.
5. **AI Usage & Costs.** Persist and present provider usage and cost telemetry without estimates
   presented as billed facts.
6. **Telegram AI & PSL.** Add explicitly initiated Telegram AI workflows and the planned PSL
   layer with privacy controls.
7. **AI Chat in TUI.** Expose the implemented AI chat capabilities in the terminal workspace.
8. **Offline HTML Redesign.** Redesign the generated single-file offline catalog while preserving
   local-only operation and safe media paths.
9. **Interactive HTML Integration.** Connect the completed local features to the portable HTML
   experience without introducing a network service.

Later-round entries are scope markers, not working controls. The UI must keep them hidden until
their supporting daemon and IPC behavior exists.
