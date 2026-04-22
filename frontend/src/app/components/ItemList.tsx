import { motion } from 'motion/react';
import { Check, Circle, Clock, Brain, CalendarClock, CalendarCheck } from 'lucide-react';

export interface ListItem {
  id: string;
  title: string;
  description?: string;
  completed?: boolean;
  priority?: 'low' | 'medium' | 'high';
  dueDate?: Date;
  countdown?: string;
}

interface ItemListProps {
  title: string;
  items: ListItem[];
  type: 'memory' | 'reminder' | 'todo' | 'deadline' | 'appointment';
  onToggle?: (id: string) => void;
  maxHeight?: string;
}

export function ItemList({ title, items, type, onToggle, maxHeight = '400px' }: ItemListProps) {
  const getIcon = () => {
    switch (type) {
      case 'memory':
        return <Brain className="w-4 h-4" />;
      case 'reminder':
        return <Clock className="w-4 h-4" />;
      case 'todo':
        return <Circle className="w-4 h-4" />;
      case 'deadline':
        return <CalendarClock className="w-4 h-4" />;
      case 'appointment':
        return <CalendarCheck className="w-4 h-4" />;
    }
  };

  const getPriorityColor = (priority?: 'low' | 'medium' | 'high') => {
    switch (priority) {
      case 'high':
        return 'var(--color-destructive)';
      case 'medium':
        return 'var(--color-chart-4)';
      case 'low':
        return 'var(--color-muted-foreground)';
      default:
        return 'var(--color-muted-foreground)';
    }
  };

  return (
    <div className="bg-background border border-border rounded-lg">
      <div className="px-4 py-3 border-b border-border flex items-center gap-2">
        <div className="text-muted-foreground">{getIcon()}</div>
        <h3 className="text-foreground">{title}</h3>
        <div className="ml-auto text-sm text-muted-foreground">{items.length}</div>
      </div>

      <div className="divide-y divide-border overflow-y-auto" style={{ maxHeight }}>
          {items.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              暂无{title}
            </div>
          ) : (
            items.map((item, idx) => (
              <motion.div
                key={item.id}
                className="px-4 py-3 hover:bg-muted/50 transition-colors cursor-pointer"
                onClick={() => onToggle?.(item.id)}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.2, delay: idx * 0.03 }}
              >
                <div className="flex items-start gap-3">
                  {type === 'todo' && (
                    <div className="mt-0.5">
                      {item.completed ? (
                        <Check className="w-4 h-4 text-primary" />
                      ) : (
                        <Circle className="w-4 h-4 text-muted-foreground" />
                      )}
                    </div>
                  )}

                  <div className="flex-1 min-w-0">
                    <div className={`text-sm ${item.completed ? 'line-through text-muted-foreground' : 'text-foreground'}`}>
                      {item.title}
                    </div>
                    {item.description && (
                      <div className="text-xs text-muted-foreground mt-1">
                        {item.description}
                      </div>
                    )}
                    {item.dueDate && (
                      <div className="text-xs text-muted-foreground mt-1 flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {item.dueDate.toLocaleDateString('zh-CN')}
                      </div>
                    )}
                    {item.countdown && (
                      <div className={`text-xs mt-1 font-medium ${
                        item.countdown.includes('⚠️') ? 'text-destructive' : 'text-muted-foreground'
                      }`}>
                        {item.countdown}
                      </div>
                    )}
                  </div>

                  {item.priority && (
                    <div
                      className="w-1.5 h-1.5 rounded-full mt-2"
                      style={{ backgroundColor: getPriorityColor(item.priority) }}
                    />
                  )}
                </div>
              </motion.div>
            ))
          )}
      </div>
    </div>
  );
}
