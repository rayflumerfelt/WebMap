/**
 * North arrow. `06-rendering.md` §9.
 *
 * **Omitted when the bearing is 0 and the user has not asked for it.** A north
 * arrow on a north-up map is decoration, and on a slide it is decoration that
 * takes space from the data. On a rotated map it is not optional: a reader who
 * assumes north-up will misread every azimuth on the page.
 */

export interface NorthArrowProps {
  /** Map bearing in degrees, clockwise. 0 is north-up. */
  bearing: number;
  /** Show even at bearing 0. For a printed map where convention expects one. */
  always?: boolean;
  size?: number;
  className?: string;
}

/** Below this, the map is north-up as far as a reader is concerned. */
const NORTH_UP_TOLERANCE_DEG = 0.5;

export function NorthArrow({ bearing, always = false, size = 32, className }: NorthArrowProps) {
  const rotation = normaliseBearing(bearing);
  if (!always && Math.abs(rotation) < NORTH_UP_TOLERANCE_DEG) return null;

  return (
    <div
      className={className}
      role="img"
      aria-label={
        Math.abs(rotation) < NORTH_UP_TOLERANCE_DEG
          ? 'North arrow: map is north-up'
          : `North arrow: map is rotated ${Math.round(rotation)} degrees`
      }
      style={{
        width: size,
        height: size,
        display: 'grid',
        placeItems: 'center',
        background: 'rgba(255, 255, 255, 0.82)',
        borderRadius: '50%',
        userSelect: 'none',
      }}
    >
      <svg
        width={size * 0.7}
        height={size * 0.7}
        viewBox="0 0 24 24"
        aria-hidden="true"
        // Counter-rotated: the map turns clockwise by `bearing`, so the arrow
        // must turn the other way to keep pointing at true north.
        style={{ transform: `rotate(${-rotation}deg)` }}
      >
        <path d="M12 2 L17 20 L12 16 L7 20 Z" fill="#242e39" />
        <text
          x="12"
          y="10"
          textAnchor="middle"
          fontSize="7"
          fontWeight="700"
          fill="#ffffff"
          fontFamily="Inter, system-ui, sans-serif"
        >
          N
        </text>
      </svg>
    </div>
  );
}

/** To (-180, 180], so a bearing of 359.9° reads as 0.1° off north rather than
 *  as a fully rotated map. */
function normaliseBearing(bearing: number): number {
  const wrapped = ((bearing % 360) + 360) % 360;
  return wrapped > 180 ? wrapped - 360 : wrapped;
}
