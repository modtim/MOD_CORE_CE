# How a release is drafted and published

This page is for the person who publishes MOD Core CE releases. No coding is needed.

## What happens by itself

A small helper called Release Drafter (its workflow file is `release-drafter.yml` inside the `.github` folder)
watches the `main` branch. Each time a pull request is merged, it updates a DRAFT release
under **Releases**. The draft lists the merged pull requests under the headings What's New,
Breaking Changes, Bug Fixes and Under the Hood, and works out the next version number from the
labels on those pull requests. It never publishes anything on its own.

The workflow is expected and safe. It runs only in this repository, uses only the built-in
`GITHUB_TOKEN`, and asks for just two permissions: write access to releases (to keep the draft)
and to pull-request labels (to tag them). If GitHub shows it as waiting for approval, that is
this workflow; it is the one you want.

## Labels

The draft sorts pull requests by label, so the labels must exist once in the repository. They
are listed with colours and descriptions in `.github/labels.yml`. To create them: open the
repository on GitHub, go to **Issues -> Labels -> New label**, and type the name, colour and
description from the file. Ten labels in all. (Someone with the GitHub CLI can instead run
`gh label create <name> --color <color> --description "<text>"` for each.)

Pull requests whose title starts with `feat:`, `fix:`, `chore:`, `docs:` or `breaking:` get
their label automatically. Pull requests from outside contributors do not (GitHub gives them a
read-only token on purpose), so add the label by hand. Add `skip-changelog` to leave one out.

## Release day

1. Open **Releases** and click the draft that Release Drafter keeps up to date.
2. Replace the grey placeholder lead paragraph (the line that starts with "WRITE ONE PLAIN
   PARAGRAPH") with a few plain sentences about what this release adds.
3. Replace the banner line with the full address of the banner image
   (`https://raw.githubusercontent.com/modtim/MOD_CORE_CE/main/docs/images/ce-banner.png`),
   because release pages do not understand repository-relative image paths (the address works once the
   repository is public; the PNG twin is 3200x800 so it stays sharp on retina screens).
4. Check the version number and the tag name at the top. Change them if the automatic number is wrong.
5. Attach the image digests list (see below).
6. Click **Publish release**.

The very first release, v1.0.0, has no earlier tag, so the draft's compare link will be
incomplete. Use the ready-made text in `docs/release-body-v1.0.0.md` for that one instead.

## Attaching the image digests list

Every published image has a content fingerprint (a digest) that lets people confirm they run
exactly what was released. After the images are published, save the list of image names and
digests to a plain text file (for example `image-digests.txt`), then on the draft click
**Attach binaries by dropping them here or selecting them** and add the file. People can then
check their pulled images against it; the SBOM check is described in [sbom.md](./sbom.md).

## If something looks wrong

- No draft appears: check the **Actions** tab for a failed "Release Drafter" run.
- A pull request is missing from the draft: it was merged without a label that has a heading,
  or carries `skip-changelog`. Add the right label; the draft updates on the next merge.
- Wrong next version: the highest label wins (`breaking` beats `feature` beats `bug`).
