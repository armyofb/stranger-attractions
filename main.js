// Renders event cards from events.js. Past shows (before today, local time)
// are hidden automatically; a show stays visible on its own day.

(function () {
  const grid = document.getElementById("events-grid");
  if (!grid || typeof EVENTS === "undefined") return;

  const MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  const DOWS = ["SUN","MON","TUE","WED","THU","FRI","SAT"];

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const upcoming = EVENTS
    .map(e => ({ ...e, when: new Date(e.date + "T00:00:00") }))
    .filter(e => e.when >= today)
    .sort((a, b) => a.when - b.when);

  if (upcoming.length === 0) {
    grid.innerHTML = '<p class="empty">No shows on the books right now — follow us on <a href="https://www.facebook.com/StrangerAttractions/" target="_blank" rel="noopener">Facebook</a> for announcements.</p>';
    return;
  }

  const esc = s => String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  const soonCutoff = new Date(today);
  soonCutoff.setDate(soonCutoff.getDate() + 7);

  grid.innerHTML = upcoming.map(e => {
    const isToday = e.when.getTime() === today.getTime();
    const isSoon = !isToday && e.when < soonCutoff;
    const badge = isToday ? '<span class="card-soon">Tonight</span>'
                : isSoon ? '<span class="card-soon">This Week</span>' : "";
    const times = [
      e.doors ? `Doors <strong>${esc(e.doors)}</strong>` : "",
      e.show ? `Show <strong>${esc(e.show)}</strong>` : ""
    ].filter(Boolean).join(" &nbsp;·&nbsp; ");

    return `
    <article class="card">
      <a class="card-poster" href="${esc(e.tickets)}" target="_blank" rel="noopener" aria-label="Tickets for ${esc(e.headliner)}">
        <img src="${esc(e.poster)}" alt="${esc(e.headliner)} show poster" loading="lazy">
        <span class="card-date">
          <span class="d-month">${MONTHS[e.when.getMonth()]}</span>
          <span class="d-day">${e.when.getDate()}</span>
          <span class="d-dow">${DOWS[e.when.getDay()]}</span>
        </span>
        ${badge}
      </a>
      <div class="card-body">
        ${e.tag ? `<span class="card-tag">${esc(e.tag)}</span>` : ""}
        <h3 class="card-title">${esc(e.headliner)}</h3>
        ${e.support && e.support.length ? `<p class="card-support">with <span>${e.support.map(esc).join("</span> · <span>")}</span></p>` : ""}
        <div class="card-meta">
          <span><strong>${esc(e.venue)}</strong></span>
          ${times ? `<span>${times}</span>` : ""}
          ${e.price ? `<span><strong>${esc(e.price)}</strong></span>` : ""}
          ${e.age ? `<span>${esc(e.age)}</span>` : ""}
        </div>
        <div class="card-actions">
          <a class="btn-tickets" href="${esc(e.tickets)}" target="_blank" rel="noopener">Get Tickets</a>
          <a class="btn-fb" href="${esc(e.facebook)}" target="_blank" rel="noopener">FB Event</a>
        </div>
      </div>
    </article>`;
  }).join("");

  // Structured data (schema.org MusicEvent) so search engines can surface
  // the shows in event listings and rich results.
  const SITE = "https://strangerattractionspresents.com";
  const ld = {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "Organization",
        "@id": SITE + "/#org",
        "name": "Stranger Attractions Presents",
        "url": SITE + "/",
        "logo": SITE + "/assets/logo.jpg",
        "email": "dustinboltjes@gmail.com",
        "sameAs": [
          "https://www.facebook.com/StrangerAttractions/",
          "https://www.instagram.com/strangerattractionsindy"
        ]
      },
      ...upcoming.map(e => ({
        "@type": "MusicEvent",
        "name": e.headliner + (e.support && e.support.length ? " w/ " + e.support.join(", ") : ""),
        "startDate": e.date,
        "url": e.facebook,
        "image": SITE + "/" + e.poster,
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
          "@type": "MusicVenue",
          "name": e.venue,
          "address": e.address
        },
        "performer": [e.headliner].concat(e.support || []).map(n => ({ "@type": "MusicGroup", "name": n })),
        "organizer": { "@id": SITE + "/#org" },
        "offers": {
          "@type": "Offer",
          "url": e.tickets,
          "price": (e.price || "").replace("$", ""),
          "priceCurrency": "USD",
          "availability": "https://schema.org/InStock"
        }
      }))
    ]
  };
  const s = document.createElement("script");
  s.type = "application/ld+json";
  s.textContent = JSON.stringify(ld);
  document.head.appendChild(s);
})();
