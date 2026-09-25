// Shared ghost-site data renderer — SiteScout / SiteScout Track B
//
// The ONE canonical rendering script every automated build injects
// (ghost_site_builder.py) — not something Cyril uploads or edits per
// template. He designs the visual layer in Paper AI and marks real data
// spots with data-slot attributes; this file is the only thing that
// ever touches those attributes at runtime.
//
// Fetches lead.json (same directory, generated fresh per lead at build
// time) and populates the DOM via textContent only — never innerHTML —
// so a business name or hero line containing HTML-special characters
// can never break the page or inject markup. Originally written for the
// Sprint 16 barber-shops template; fully generic (no niche-specific
// logic), so it now serves every niche's template unchanged.
//
// Data-slot contract, matching ghost_site_lead_data.py's output:
// business_name, city, phone, hero_line, services, hours, review_count,
// review_rating.
//
// Missing-data handling (2026-08-16, ahead of the planned Apollo lead
// source — Places API gives hours/reviews reliably, Apollo won't):
// confirmed with Cyril which of two very different things "sample
// content" should mean, since one of them re-opens exactly the
// fabricated-content risk Sprint 16 found and rejected. Landed on:
// honest generic wording that never claims to be the business's real
// data, never a specific fabricated fact (no invented hours, no
// invented star rating). Split by risk, not treated uniformly:
//   - Identity fields (business_name, city) are never faked — Apollo
//     leads always have a real business name anyway.
//   - Phone: a wrong number actively misdirects a real caller, so a
//     missing one hides the call-to-action rather than showing
//     anything at all.
//   - Hours / reviews / hero line are genuinely decorative. Missing
//     data there gets honest placeholder copy instead of being hidden.

const SAMPLE_TEXT = {
  hero_line: "Proudly serving the local community.",
  hours: "Hours available upon request — contact us to confirm.",
  reviews: "New here — ask us about our work directly.",
};

async function loadLead() {
  const res = await fetch("lead.json");
  if (!res.ok) {
    console.error("Could not load lead.json:", res.status);
    return;
  }
  const lead = await res.json();
  render(lead);
}

function render(lead) {
  document.querySelectorAll("[data-slot]").forEach((el) => {
    const key = el.getAttribute("data-slot");
    if (lead[key] !== undefined && lead[key] !== null && lead[key] !== "") {
      el.textContent = lead[key];
    } else if (key === "hero_line") {
      el.textContent = SAMPLE_TEXT.hero_line;
    }
  });

  document.querySelectorAll("[data-slot-href]").forEach((el) => {
    const key = el.getAttribute("data-slot-href");
    if (key !== "phone_tel") return;
    if (lead.phone) {
      el.setAttribute("href", "tel:" + lead.phone.replace(/[^\d+]/g, ""));
    } else {
      // A wrong or missing phone actively misdirects a real caller —
      // unlike hours/reviews, there's no honest placeholder for this,
      // so the call-to-action just doesn't render at all.
      el.style.display = "none";
    }
  });

  renderHours(lead.hours);

  const trustSection = document.querySelector('[data-slot-hidden-if-empty="review_count"]');
  if (trustSection && !lead.review_count) {
    trustSection.textContent = SAMPLE_TEXT.reviews;
  }

  if (lead.business_name) {
    document.title = lead.business_name;
  }
}

function renderHours(hours) {
  const list = document.querySelector('[data-slot-list="hours"]');
  if (!list) return;
  while (list.firstChild) {
    list.removeChild(list.firstChild);
  }
  if (!hours || hours.length === 0) {
    const li = document.createElement("li");
    li.className = "hours-row";
    li.textContent = SAMPLE_TEXT.hours;
    list.appendChild(li);
    return;
  }
  hours.forEach((line) => {
    // Each entry arrives as "Day: open-close" — split on the first colon
    // so day and hours render in their own columns.
    const li = document.createElement("li");
    li.className = "hours-row";
    const separatorIndex = line.indexOf(":");
    const day = separatorIndex === -1 ? line : line.slice(0, separatorIndex).trim();
    const time = separatorIndex === -1 ? "" : line.slice(separatorIndex + 1).trim();
    const dayEl = document.createElement("span");
    dayEl.textContent = day;
    const timeEl = document.createElement("span");
    timeEl.textContent = time;
    li.appendChild(dayEl);
    li.appendChild(timeEl);
    list.appendChild(li);
  });
}

loadLead();
