# labels: analyst inputs

These files are read on every run (`shadowleads validate`). Analyst decisions override every
automatic rule.

| File | Purpose |
|---|---|
| `match_audit.csv`, `match_audit_2.csv`, … | Stratified samples of place → company links (from `shadowleads audit-sample --out labels/match_audit_N.csv`, each excluding links already audited). Set `verdict` to `correct` or `wrong`, and leave it empty when undetermined. Write in `note` how you checked. Wrong links are excluded from leads, and the audit precision is reported. |
| `link_overrides.csv` | Final decisions: `set` links a place to the given company code, `reject` removes a link. `reason` records the evidence, e.g. "privacy policy names X". |

## How a link is validated: levels of fallback

Each level is used when the previous ones cannot decide. Levels 0–3 are automatic; level 4 is what
an analyst does during an audit. Every level 4 method below comes from the audit notes.

**Level 0 – name and address rules (automatic)**
- The exact full name matches a JAR legal name or a VMI branch trade name, corroborated by a
  fitting activity code or the same building as the registered address.
- A core name match (generic words removed) needs the address, or a rare name plus a fitting
  activity code.
- Address only: one consistent company at a single-tenant Vilnius address. This is the weakest
  rule (67% precision in audit 2).

**Level 1 – independent official or self-declared evidence (automatic)**
- A company or VAT code published on the business's own website. When the privacy-policy or terms
  page carries a code, that code wins over codes elsewhere on the site.
- The VMVT food-premises register: company code at the same premises.
- The Google "website" is a company-directory page (rekvizitai URL).
- A company code quoted in Google search snippets that also mention the business name.

**Level 2 – brand-level evidence (automatic, never enough on its own)**
- The trademark owner (State Patent Bureau, LINTA) and job-ad employers. These name the company
  behind a brand, which may be a franchisor or a group company rather than the venue operator.

**Level 3 – plausibility checks (automatic)**
- The company is active, registered in Vilnius city, and its activity code fits the category.
- The company is not spread implausibly across unrelated places.
- The website is not shared by 3 or more locations (a franchisor's or brand's site).
- The Google listing is not an erroneous entry, for example a name that is only an address.

**Level 4 – analyst verification (during an audit or before an inspection)**

Some of these checks also run automatically at levels 1 and 3; the table shows which part is
automated and what the analyst adds when automation cannot decide.

| Check | Automated (pipeline) | Analyst |
|---|---|---|
| **Website footer** – the operator is often named at the bottom of the site ("Top Servisas" → UAB "Meistrų centras") | the scanner reads the homepage and finds a company / VAT code if it is in the page text | opens the site when no code was found (code shown as an image, page rendered by JavaScript, footer only on inner pages) |
| **Privacy policy / terms of service** – name the data controller, i.e. the operator (ozopadangos.lt → UAB "Padangų parkas", 4play.lt → UAB "Doriantas", olysportsbar.lt → UAB "MECOM GRUPP") | follows up to 3 links (contacts, requisites, privacy, terms); a code on a privacy / terms page outranks other codes on the site | reads the privacy policy when the scanner did not reach it, and prefers it over shop terms: one site can serve an e-shop company and a salon company (Synth: MB Šilaika runs the shop, MB Du Vilkai the hairdresser) |
| **Phone number match** – the Google phone equals the company's phone | – (candidate: compare with the phone on the business's own website) | looks the phone up in a company directory such as scoris.lt or rekvizitai.lt (manual only; directories forbid scraping) |
| **Only business of its kind at the address** | address-only links require a single category-consistent company at a non-multi-tenant address | checks the building on the map: a single beauty salon there supports the link even when names differ |
| **Google listing sanity** | listings named only by an address are out of scope | rejects other erroneous entries, e.g. a foreign-language placeholder at a company's address ("Super tanie jedzenie") |
| **Scale plausibility** | multi-site companies are flagged; national chains are excluded from peer medians | rejects a national company as the operator of a single venue (a brewery behind the "Švyturys" bar name); accepts chains (H2Auto, Inter Cars, Carglass) knowing their figures cover every location |
| **Franchise check** | a website shared by 3+ locations is treated as a franchisor or brand site; brand-level evidence never links alone | leaves the verdict undetermined unless the location's operator is confirmed (PRO BRO Express / Švaros broliai) |
| **Premises licence** – the hygiene passport of beauty / cosmetology premises shows holder and address | – (LIS forbids copying) | looks it up in the LIS register (licencijavimas.lt): it settles whether the website operator also runs the venue or a specialist works there under individual activity (MB DanNik / Jekaterinos Depiliacija) |

**Level 5 – record the decision.** Put the verified company (or a rejection) and the evidence used
in `link_overrides.csv`, so it applies to every future run.

Candidates for automating level 4:
- **Phone match:** Google phone vs the phone on the business's own website.
- **Deeper website scans** that always fetch the privacy-policy page: the scanner currently stops
  after 4 pages, which is why Synth still needed an override.

Not used: comparing the company's age with the venue's age. Operators change, companies are
re-registered and venues move for many legitimate reasons, so it does not identify the operator.
