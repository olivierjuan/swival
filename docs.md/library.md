# The Library

The library is a personal shelf for things you've downloaded but haven't necessarily switched on. It lives under your global config directory at `~/.config/swival/library/` (or `$XDG_CONFIG_HOME/swival/library/`), and the agent never loads anything from it directly. You stage a collection there once, look it over, and then install the pieces you actually want into a project or into your global active set.

Today the library holds skills, under `library/skills/`. The name was chosen with room to grow: it is meant to become the staging area for other global resources you might pull in from elsewhere and selectively add to a project later. For now this page is about skills, which is the part that exists.

## Why A Staging Shelf

There are three places a skill can end up. Two are active, meaning the agent discovers and can load them:

- `.swival/skills/` is active in this project only.
- `~/.config/swival/skills/` is active in every project.

The third is the library:

- `~/.config/swival/library/skills/` is staged, not active. The agent does not see these.

The split exists so that fetching a third party's repository and turning their instructions loose in every session are two separate, deliberate steps. You can download a collection, read the `SKILL.md` files at your leisure, and only then promote the ones you trust. Nothing in the library influences a run until you install it somewhere active.

The library is organized two levels deep: `library/skills/<collection>/<skill>/SKILL.md`. A collection is just the set of skills that came from one source, named after the repository it was cloned from unless you rename it.

## The Commands

```sh
swival skills add    [--global] [--as NAME] [--ref REF] [--force] <name-or-URL>
swival skills delete [--global] [--library] <name>
swival skills list   [--library]
```

The examples below all use [`DietrichGebert/ponytail`](https://github.com/DietrichGebert/ponytail), a repository whose top-level `skills/` directory ships six skills (`ponytail`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, `ponytail-help`, `ponytail-review`).

## Staging A Collection

Point `add --global` at a git repository and Swival shallow-clones it, finds the `skills/` directory, and copies what it finds into the library. It does not activate anything.

```sh
swival skills add --global https://github.com/DietrichGebert/ponytail
```

```
Staged 6 skill(s) into the library (~/.config/swival/library/skills/ponytail):
  ponytail, ponytail-audit, ponytail-debt, ponytail-gain, ponytail-help, ponytail-review
Run 'swival skills add ponytail' to install them into this project,
or 'swival skills add --global ponytail' to install them globally.
```

The collection takes its name from the repository (`ponytail`). Override that with `--as`, which is handy when you want to stage the same repo at two different points in time, or just prefer a shorter name:

```sh
swival skills add --global --as pony https://github.com/DietrichGebert/ponytail
```

Pin a specific branch, tag, or commit with `--ref` (or append `#ref` to the URL). This is the safe way to depend on a third party: a moving `main` can change under you, a pinned commit cannot.

```sh
swival skills add --global --as pony-stable --ref main https://github.com/DietrichGebert/ponytail
```

A repository whose `SKILL.md` sits at the root, rather than under `skills/`, is treated as a single skill and staged as a one-skill collection.

## Listing What You Have

`list --library` shows the staged collections, where each came from, and the skills inside them. If a collection was pinned, the ref is shown after the URL.

```sh
swival skills list --library
```

```
skills library (~/.config/swival/library/skills):
  pony  [https://github.com/DietrichGebert/ponytail]
    ponytail
    ponytail-audit
    ponytail-debt
    ponytail-gain
    ponytail-help
    ponytail-review
  pony-stable  [https://github.com/DietrichGebert/ponytail @ main]
    ...
```

Plain `list` (no flag) shows the active project and global skills instead, which is the set the agent will actually load.

## Promoting From The Library

Once a collection is staged, install from it by name. Without `--global` the skill lands in `.swival/skills/` for the current project; with `--global` it lands in your global active set.

```sh
swival skills add ponytail-review            # one skill, into this project
swival skills add --global ponytail-gain     # one skill, active everywhere
swival skills add ponytail                   # the whole collection, into this project
```

Installing a single skill reports exactly what moved:

```
Installed 1 skill(s) into this project (.swival/skills): ponytail-review
```

Installing a collection where one skill is already present skips the duplicate and installs the rest:

```
⚠ Warning: skill 'ponytail-review' already exists in .swival/skills; use --force to replace
Installed 5 skill(s) into this project (.swival/skills): ponytail, ponytail-audit, ponytail-debt, ponytail-gain, ponytail-help
```

Pass `--force` to overwrite instead of skip:

```sh
swival skills add --force ponytail
```

If the same skill name exists in more than one staged collection, a bare name is ambiguous and Swival refuses rather than guess:

```
Error: skill 'ponytail-review' is ambiguous across collections:
pony/ponytail-review, pony-stable/ponytail-review; disambiguate with 'collection/skill'
```

Spell out the collection to resolve it:

```sh
swival skills add pony/ponytail-review
```

## Skipping The Library

If you already trust a repository and don't want a review step, point `add` at the URL without `--global`. Swival clones it and installs straight into the current project, no staging:

```sh
swival skills add https://github.com/DietrichGebert/ponytail
```

```
Installed 6 skill(s) into this project (.swival/skills): ponytail, ponytail-audit, ponytail-debt, ponytail-gain, ponytail-help, ponytail-review
```

This is the shortcut. The staged flow exists for when you want to look before you leap.

## Removing Things

Plain `delete` only ever touches active skills, never the library. You address an active skill by name:

```sh
swival skills delete ponytail-debt           # from .swival/skills/
swival skills delete --global ponytail-gain  # from ~/.config/swival/skills/
```

To remove something from the library you must say so with `--library`. You can drop one skill out of a collection, or the whole collection at once:

```sh
swival skills delete --library pony/ponytail-help   # one skill
swival skills delete --library pony                 # the whole collection
```

Deletion happens immediately, with no confirmation prompt, but Swival always prints exactly what it removed:

```
Removed skill pony/ponytail-help from the library
Removed collection 'pony' from the library
```

## A Note On Safety

Cloning and staging never execute anything. Helper scripts and `SKILL.star` metaskills that ship inside a skill sit inert until you both install the skill and, for external metaskills, pass `--metaskills all`. Swival flags any installed skill that carries a `SKILL.star`, so a downloaded metaskill can't quietly become runnable. Clones run with credential prompts disabled, HTTP redirects refused, and non-git protocols blocked, and any HTTP(S) URL that resolves to a private or internal address is rejected outright (an explicit localhost host is exempt).

For everything about authoring skills, discovery precedence, and progressive disclosure, see the [Skills](skills.md) page. For the metaskill format and host API, see [Agent MetaSKILLs](metaskills.md).
