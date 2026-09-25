// Barber Shops — Ghost Site template (Sprint 16/17)
//
// Fetches lead.json (same directory) and populates the DOM via
// textContent only — never innerHTML — so a business name or hero line
// containing HTML-special characters can never break the page or inject
// markup. This is the data-slot contract ghost_site_lead_data.py
// produces: business_name, city, phone, hero_line, services, hours,
// review_count, review_rating. Sprint 17's builder just needs to drop a
// lead.json next to these files — no server-side templating step.

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
    }
  });

  document.querySelectorAll("[data-slot-href]").forEach((el) => {
    const key = el.getAttribute("data-slot-href");
    if (key === "phone_tel" && lead.phone) {
      el.setAttribute("href", "tel:" + lead.phone.replace(/[^\d+]/g, ""));
    }
  });

  renderHours(lead.hours);

  const trustSection = document.querySelector('[data-slot-hidden-if-empty="review_count"]');
  if (trustSection && !lead.review_count) {
    trustSection.style.display = "none";
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
    list.style.display = "none";
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
