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

**Level 4 – manual verification (analyst, during an audit)**
1. **Website footer:** the operating company is often named at the bottom of the business's own
   website ("Top Servisas" → UAB "Meistrų centras").
2. **Privacy policy or terms of service:** they name the data controller, i.e. the operator
   (ozopadangos.lt → UAB "Padangų parkas", 4play.lt → UAB "Doriantas", olysportsbar.lt → UAB "MECOM
   GRUPP"). Prefer the privacy policy over shop terms: one website can serve an e-shop company and a
   salon company (Synth: MB Šilaika runs the shop, MB Du Vilkai the hairdresser).
3. **Phone number match:** the phone on the Google listing equals the company's phone in a company
   directory such as scoris.lt or rekvizitai.lt (manual lookup only; directories forbid scraping).
4. **Only business of its kind at the address:** a single beauty salon in the building supports an
   address-only link even when the names differ.
5. **Google listing sanity:** reject erroneous entries, e.g. a name that is only an address, or a
   foreign-language placeholder at a company's address ("Super tanie jedzenie").
6. **Scale plausibility:** a national company (a brewery behind the "Švyturys" bar name) is the
   brand owner, not the bar operator. Multi-location chains (H2Auto, Inter Cars, Carglass) can be
   correct links, but their declared figures cover every location.
7. **Franchise check:** a franchised location may be run by a different company from the brand.
   Leave it undetermined unless the operator is confirmed (PRO BRO Express / Švaros broliai).
8. **Premises licence:** for beauty and cosmetology premises, the hygiene passport in the LIS
   licence register (licencijavimas.lt) shows the holder and the premises address. This settles
   whether the website operator also runs the venue, or a specialist works there under individual
   activity (MB DanNik / Jekaterinos Depiliacija). Manual lookup only: LIS forbids copying.

**Level 5 – record the decision.** Put the verified company (or a rejection) and the evidence used
in `link_overrides.csv`, so it applies to every future run.

Candidates for automating level 4:
- **Phone match:** Google phone vs the phone on the business's own website.
- **Deeper website scans** that always fetch the privacy-policy page: the scanner currently stops
  after 4 pages, which is why Synth still needed an override.

Not used: comparing the company's age with the venue's age. Operators change, companies are
re-registered and venues move for many legitimate reasons, so it does not identify the operator.
