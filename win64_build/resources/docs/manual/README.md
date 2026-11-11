# LanguageTrainer

LanguageTrainer is a local application for learning vocabulary inside a living context. You type word lists, definitions, book passages, and saved quotations instead of turning every difficulty into an isolated flashcard.

The application is designed for continuous work. You stay inside the same book, topic, or collection, notice a difficult place, and decide where it should return. A familiar word remains in Sprint until recognition becomes automatic. A word that needs semantic work moves to Meanings, where its definitions, different senses, and examples can be read and typed repeatedly. A word that is almost known but still unstable moves to scheduled Reminders. Material is not lost and the learner does not need to stop the current reading session to solve every difficulty immediately.

The goal is not to loop over the same list forever. The goal is to keep moving through meaningful text while difficult material is postponed, revisited, understood, and eventually removed from active study.

Current application version: `0.5.0`.

Each new user starts with three vocabulary lists and two books: *Zen* and *Thinking, Fast and Slow*. Existing users receive the second book once; existing copies and reading progress are preserved.

> Installation, Docker, command-line administration, build, and release details live in [DEVELOPERS.md](../../DEVELOPERS.md).

## The Learning Workflow

1. Open a dictionary or a book.
2. Type the current words, definitions, passage, or quotations.
3. After the run, inspect the words in context.
4. Leave recognizable words in Sprint, send unstable words to Reminders, and send semantically difficult words to Meanings.
5. Read definitions and examples as many times as necessary. A word may have several senses; learning only one translation is often not enough.
6. Mark a word learned only when it is genuinely familiar. It then becomes globally known for that user and language and stops wasting time in overlapping collections.
7. If a word was marked learned too early, return it through the global word list.

This workflow separates progress from perfection. You can continue reading today and let the application bring the difficult material back tomorrow.

## Learn Through Material Worth Your Time

The central idea of the program is to learn a language through material that is genuinely pleasant and interesting: original books, important works, thoughtful essays, interviews, and speeches by remarkable people. Language is easier to return to when the subject itself deserves attention.

The program cannot choose that purpose for you. The user's responsibility is to find authors, people, works, and subjects that are worth thinking about and enjoyable to read, and then reserve regular time for them. Twenty or thirty focused minutes a day is enough to maintain continuity if the practice is consistent.

Sessions do not need to be long or perfectly completed. The application stores reading progress, collection state, reminders, meanings, quotations, and difficult words, so you can stop at almost any moment. Next time you return to the same context and continue without rebuilding the session or losing momentum.

## Game Modes

Only the activities in this section are game or training modes. `Collections`, `Control`, and `Info` are application pages, not modes.

### Studying Words

The dictionary modes work with individual words while preserving the collection and context in which each word appeared.

#### Sprint

Sprint is the main recognition mode. It draws active words from one dictionary or from several selected dictionaries and asks you to type them. Use it to make spelling and recognition fast and automatic.

After a run, each word can be:

- left in Sprint;
- marked learned;
- scheduled as a Reminder;
- moved to Meanings;
- marked as a relic;
- pronounced or opened in the built-in dictionary panel.

Meanings and Reminders do not remove a word from Sprint automatically. These actions can coexist. Relic is exclusive: relic words are not learned and do not participate in training.

#### Meanings

Meanings is for words that cannot be learned reliably from spelling alone. The application shows definitions and examples and asks for the corresponding words.

Repeatedly reading a definition matters. A difficult word becomes stable when its meaning can be recovered in different wording and in several examples, not merely when one translation looks familiar. Keep a word in this mode until the important senses are recognizable without guessing.

Meanings can run across all dictionaries selected on the Collections page. Every result still belongs to its original dictionary, so changing its status updates the correct source.

#### Reminder

Reminder is scheduled repetition for words that are already partly familiar. Due words return according to `reminder_progression`. Completing a run advances them to the next interval.

Use Reminders for material that should not interrupt current reading but must not disappear. A combined Reminder run uses every selected dictionary and preserves each word's original collection.

### Reading Books

The book modes preserve longer context. `Read` moves through the original work; `Quotes` returns to passages selected from it.

#### Read

Read presents a book as sentence-aware chunks. The text is split into complete phrases within the configured minimum and maximum size. Avoid very small limits: at least 100 characters is required, and 150 or more usually gives better context.

You type the current chunk, compare it with the original, inspect unfamiliar words, and continue through the book. Progress is stored per user. The reading page also lets you highlight complete quotations for later practice.

#### Quotes

Quotes trains passages saved while reading. Quotes may be shown in book order or in full random order. Random mode deliberately removes the surrounding sequence: only the quotations that remain active participate.

Quotes can run across all selected books. Words discovered in a quotation are still attached to that book's linked dictionary.

## Words And Their States

A word belongs to a local dictionary, while learned knowledge is also tracked globally for a user and language.

- **Sprint**: active recognition practice.
- **Meanings**: semantic practice with definitions and examples.
- **Reminder**: scheduled repetition.
- **Learned**: globally known and filtered from overlapping collections.
- **Forgotten**: previously learned, but returned to active study.
- **Relic**: intentionally excluded without claiming that it is known.

Names, OCR fragments, irrelevant forms, and material you never want to learn are good relic candidates. A relic can be reviewed later in `Info` or in the collection details.

## Collections Page

The first table contains every dictionary, including dictionaries linked to books. Book dictionaries are shown as `book name words | book | language`: the first segment opens the words and `book` opens the source text.

The second column selects dictionaries for combined Sprint, Meanings, and Reminder runs. The selection is stored per user. `All` clears the selection when at least one row is active and selects every row when none is active.

The Books table shows reading progress, added words, studying words, and quotations. Its selection controls combined Quotes runs. Book buttons use `book name | words | language`.

## Control Page

Control contains three administrative tabs:

- `Add dictionary`;
- `Add text`;
- `Remove`.

These are management tools, not game modes.

### Adding A Dictionary

Enter a name and language, then paste text, select a UTF-8 file, or drop it into the editor. The format is one word or phrase per line:

```text
compliance
resilient
service level agreement
```

Spaces inside phrases are accepted. The preview counts globally known, currently studying, and new entries. Invalid characters are highlighted; errors contain line, column, and context and can move the cursor to the problem. `Remove broken lines` edits the source immediately.

### Adding A Book

Enter the title, language, and full text. The preview displays sentence-aware chunks separated as future reading pages. Adjust the minimum and maximum size, apply the preview, and add the book when the result is suitable. A linked words dictionary is created automatically.

### Removing Data

The Remove tab lists all dictionaries and books. Multiple rows can be selected and deleted after confirmation. Removing a book also removes its linked words, progress, and quotations.

The red user-removal section requires the exact, case-sensitive name of the current user. It refuses an incorrect name and refuses attempts to delete another account.

## Info Page

Info is a global inspection page, not a training mode. It shows:

- combined Reminder statistics for selected dictionaries;
- the paginated global list of learned and forgotten words;
- source collections for globally known words;
- a paginated global relic table.

Actions performed here still update the word in its original collection.

## Books, Quotations, And Context

Each book stores its original text, generated chunks, reading progress, quotations, and a linked words dictionary. Saved highlights expand to complete sentence boundaries. Quotations can be activated, muted, or deleted from the book details.

The linked dictionary is important: vocabulary remains connected to the text where it was found. You can return from a word to the book, reread the passage, and reinforce meaning through context instead of memorizing an isolated pair.

## Per-User Settings

Settings are stored in `user_settings.json`. Existing values are preserved when the application is upgraded; newly introduced fields are added with defaults. Collection selections, strict typing, screen keyboard, random quotations, chunk sizes, run sizes, and Reminder intervals are user-specific.

An old `0.5.0` database is upgraded on startup. The application adds the `word.in_sprint` field and initializes it without placing learned or relic words into Sprint. Back up `dict.sqlite3` before any release upgrade even though migrations are automatic.
