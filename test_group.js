const events = [
    {id: 1, start_time: '2026-04-10T15:50:00', end_time: '2026-04-10T16:00:00'},
    {id: 2, start_time: '2026-04-10T16:00:00', end_time: '2026-04-10T16:20:00'},
    {id: 3, start_time: '2026-04-10T16:00:00', end_time: '2026-04-10T16:03:00'},
    {id: 4, start_time: '2026-04-10T16:30:00', end_time: '2026-04-10T19:10:00'},
    {id: 5, start_time: '2026-04-10T18:00:00', end_time: '2026-04-10T19:00:00'},
    {id: 6, start_time: '2026-04-10T16:15:00', end_time: '2026-04-10T17:00:00'},
    {id: 7, start_time: '2026-04-10T17:00:00', end_time: '2026-04-10T18:30:00'},
    {id: 8, start_time: '2026-04-10T18:30:00', end_time: '2026-04-10T19:10:00'},
    {id: 9, start_time: '2026-04-10T19:20:00', end_time: '2026-04-10T19:20:00'},
    {id: 10, start_time: '2026-04-10T18:00:00', end_time: '2026-04-10T19:00:00'},
    {id: 11, start_time: '2026-04-10T18:00:00', end_time: '2026-04-10T19:15:00'},
    {id: 12, start_time: '2026-04-10T19:20:00', end_time: '2026-04-10T20:40:00'},
    {id: 13, start_time: '2026-04-10T19:20:00', end_time: '2026-04-10T20:40:00'},
    {id: 14, start_time: '2026-04-10T17:30:00', end_time: '2026-04-10T18:30:00'},
    {id: 15, start_time: '2026-04-10T18:30:00', end_time: '2026-04-10T19:10:00'},
    {id: 16, start_time: '2026-04-10T19:46:00', end_time: '2026-04-10T20:40:00'},
    {id: 17, start_time: '2026-04-10T20:00:00', end_time: '2026-04-10T20:40:00'},
    {id: 18, start_time: '2026-04-10T20:40:00', end_time: null}
];

events.sort((a,b) => a.start_time.localeCompare(b.start_time));

function groupOverlapping(events) {
    if (!events.length) return [];
    const groups = [];
    let i = 0;
    while (i < events.length) {
      const group = [events[i]];
      let maxEnd = events[i].end_time ? new Date(events[i].end_time) : null;
      let j = i + 1;
      while (j < events.length && maxEnd) {
        const nextStart = new Date(events[j].start_time);
        if (nextStart < maxEnd) {
          group.push(events[j]);
          const ne = events[j].end_time ? new Date(events[j].end_time) : null;
          if (ne && ne > maxEnd) maxEnd = ne;
          j++;
        } else break;
      }
      groups.push(group);
      i = j;
    }
    return groups;
}

const groups = groupOverlapping(events);
let count = 0;
groups.forEach(g => count+=g.length);
console.log(`Groups: ${groups.length}, Total rendered events: ${count}, Expected: ${events.length}`);
