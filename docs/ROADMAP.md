# Hoard implementation roadmap

Hoard is developed in six bounded rounds. A round is complete only after local verification,
wheel and privacy review, normal fast-forward publication to `origin/main`, remote-HEAD
verification and a clean local worktree. GitHub Actions is retained for manual dispatch only and
is not required for round completion; it must not be run without separate user permission.

1. **AI Core — completed.** Provider-neutral requests and responses, DeepSeek transport, JSON
   validation, privacy limits and the independent AI job pool.
2. **Textual TUI Foundation — completed.** Strict black theme, modular app/screens/widgets,
   responsive keyboard navigation and bounded activity polling over existing IPC.
3. **Telegram Workspace — completed.** Authorization, collector controls, source management,
   targeted scans, archive/profile/job views and catalog operations through daemon IPC.
4. **AI Infrastructure — completed.** Versioned prompts, persistent chats, explicit scoped memory,
   deterministic context assembly, durable attempts, provider usage, Decimal tariff snapshots,
   statistics, bounded IPC and private attachment references.
5. **Telegram AI + PSL + AI Chat TUI — completed.** Explicitly confirmed AI chats, provider and
   prompt controls, scoped memory, tariffs/statistics, durable manual retries, saved-profile photo
   selection and the versioned adult-only subjective PSL contract.
6. **Offline HTML Experience.** Complete the offline redesign and interactive single-file catalog
   without introducing a web service.

The accelerated plan preserves every prior scope marker: old rounds 4 and 5 are merged into round
4; old rounds 6 and 7 are merged into round 5; old rounds 8 and 9 are merged into round 6.
Later-round controls stay hidden until their complete daemon and interface behavior exists.
