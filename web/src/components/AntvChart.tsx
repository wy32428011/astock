import { Column, Line, Pie, type ColumnOptions, type LineOptions, type PieOptions } from '@antv/g2plot';
import { useEffect, useRef } from 'react';

type AntvChartProps =
  | { type: 'line'; options: LineOptions }
  | { type: 'column'; options: ColumnOptions }
  | { type: 'pie'; options: PieOptions };

// AntV G2Plot 图表生命周期封装，负责创建和销毁实例。
export function AntvChart(props: AntvChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!containerRef.current) return undefined;

    const baseOptions = {
      autoFit: true,
      height: 280,
      animation: false,
    };
    const plot =
      props.type === 'line'
        ? new Line(containerRef.current, { ...baseOptions, ...props.options })
        : props.type === 'column'
          ? new Column(containerRef.current, { ...baseOptions, ...props.options })
          : new Pie(containerRef.current, { ...baseOptions, ...props.options });

    plot.render();
    return () => {
      plot.destroy();
    };
  }, [props]);

  return <div className="chart-surface" ref={containerRef} />;
}
