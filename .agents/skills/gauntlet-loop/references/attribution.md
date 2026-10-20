# Attribution and Modification Notice

This Codex adaptation is based on **Gauntlet Loop** by Jay E / RoboNuggets:

- Source: https://github.com/robonuggets/gauntlet-loop
- Adapted from commit: `9b1975a1b8f01981f3f1e6b667ad3aaf907178ea`
- Original license: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
- Original disclaimer: the licensed material is provided as-is, without warranties, under the CC BY 4.0 terms.
- Technique and original prompt credited by the source project to Matt Shumer.

Modified for Codex on 2026-08-08. This adaptation replaces Claude-specific `/loop` and `ultracode` instructions with Codex-native subagent orchestration; adds persistent state, progress rendering, resumability, strict critic I/O, deterministic gates, neutral artifact preparation, whole-artifact final judgment, and blocker/safety handling; and changes the default from prompt generation to direct execution when the user requests work.

This adaptation is not endorsed by the original authors. Retain this notice and the CC BY 4.0 link when sharing modified or unmodified portions of the original material.
