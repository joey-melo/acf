# Skill: Reporting

## Purpose

Write concise, technically accurate, and reproducible vulnerability reports suitable for bug bounty submissions.

Reports must prioritize demonstrated impact, verifiable evidence, and clarity for security triagers. Every claim must be supported by captured evidence or a reproducible verification command.

## General Rules

- Write for the triager, not for a research audience.
- Use short sentences, plain language, and one idea per paragraph.
- Prioritize concrete impact over technical complexity.
- Remove unnecessary background, speculation, and repetition.
- Never invent commands, results, evidence, or exploit outcomes.
- Do not present expected output as captured output.
- Clearly distinguish confirmed behavior from unverified assumptions.
- Keep the report self-contained. Do not reference previous findings or reports identified in this project.

## Title

- Maximum 10 words.
- Include both the vulnerability class and its security impact.
- Use a clear, headline-style description.
- Plain text only: no Markdown formatting, backticks, or file paths.

## Description

Explain the vulnerability, its root cause, and how it can be exploited.

- Identify every relevant binary, file, path, configuration, and component by its exact name.
- Include one-line verification commands for claims that a triager cannot directly observe.
- Use only real output captured during testing.
- Prefer short code blocks and direct before/after comparisons over lengthy explanations.
- Explain the vulnerable behavior and resulting capability.
- Avoid discussing what the vulnerability is not, what was not attempted, or unsupported impact.
- Mention scope limitations only when necessary to avoid misleading the triager.

## Impact

Describe what an attacker can accomplish and why it matters.

- Maximum two paragraphs.
- No dates.
- Lead with concrete attacker capabilities and demonstrated security consequences.
- Include exploit-chain consequences only when supported by evidence.
- Avoid hypothetical escalation or exaggerated severity.
- If classification requires justification, cite the relevant wording from the program's guidelines.
- Address the most likely classification objection briefly, within this section.

## Reproduction

Reproduction must be straightforward and independently verifiable.

- Start with the single command that reproduces the vulnerability whenever possible.
- Follow with additional steps only when necessary.
- Include exact commands, paths, parameters, and prerequisites.
- Provide real captured output demonstrating successful exploitation.
- If a PoC script is attached, describe its exact success output.
- Execute and verify the PoC before submitting the report.
- Ensure the instructions work from a clearly defined starting environment.

## Report Structure

Follow this section order exactly:

1. Description
2. Impact
3. Mobile App Version
4. Setup Steps
5. Reproduction Steps

Additional requirements:

- Do not add separate Evidence or Recommended Fix sections.
- Do not reference local evidence directories or internal research artifacts.
- Include supporting commands, output, and evidence directly in the relevant sections.
- Use "N/A" for mandatory sections that do not apply.
- Do not add unnecessary sections.

## Final Validation

Before submitting a report, verify that:

- The title is within 10 words and communicates impact.
- Every technical claim is demonstrated or independently verifiable.
- All quoted output was captured during actual execution.
- Every referenced artifact has an exact identifier or path.
- The reported impact matches demonstrated capabilities.
- The reproduction steps have been tested successfully.
- The report contains no unnecessary speculation or defensive explanations.
- The report is self-contained and follows the required structure.

If essential evidence is missing, perform additional validation before finalizing. If validation is not possible, clearly identify the uncertainty rather than presenting an unsupported claim as fact.

## Guiding Principle

A triager should understand the vulnerability, recognize its impact, and reproduce it with minimal effort.

Prefer a short, fully verified report over a long report filled with explanations.